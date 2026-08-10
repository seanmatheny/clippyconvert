"""Fetch "My Clippings.txt" from a USB-connected Kindle.

Two transport modes, tried in order:

1. USB mass storage — older Kindles mount as a volume (e.g. /Volumes/Kindle)
   with the clippings file at documents/My Clippings.txt.
2. MTP — newer Kindles (2024+ firmware, Scribe, etc). Calls libmtp directly
   via ctypes (`brew install libmtp`).

Two hard-won constraints shape the MTP code:

- ONE session per plug-in: the Scribe drops off the USB bus entirely when an
  MTP session closes (even a bare `mtp-detect`) and won't re-enumerate until
  physically replugged. So connect, list, and fetch must all happen in a
  single session — shelling out to mtp-detect/mtp-files/mtp-getfile (a
  session each) can never work.
- No full-device listing: LIBMTP_Get_Filelisting enumerates every object on
  the device; on a Scribe (thousands of notebook/book objects) the device's
  MTP stack gives up partway through, drops the connection, and libmtp spins
  at 100% CPU. Instead, list just the storage root, then just the folders on
  it (documents first), looking for the clippings file.
"""

import ctypes
import ctypes.util
import glob
import os
import re
import shutil
import tempfile

CLIPPINGS_NAME = "my clippings.txt"
KINDLE_DEVICE_RE = re.compile(r"kindle|scribe|amazon|lab126", re.IGNORECASE)

_LIBMTP_CANDIDATES = (
    "/opt/homebrew/lib/libmtp.dylib",
    "/usr/local/lib/libmtp.dylib",
)
_PARENT_ROOT = 0xFFFFFFFF  # LIBMTP_FILES_AND_FOLDERS_ROOT
_FILETYPE_FOLDER = 0  # LIBMTP_FILETYPE_FOLDER
_MAX_FOLDER_SCANS = 20


class KindleFetchError(Exception):
    pass


def _fetch_mass_storage(dest: str):
    """Older Kindles mount as a USB drive."""
    for vol in glob.glob("/Volumes/*"):
        for sub in ("documents", "Documents"):
            d = os.path.join(vol, sub)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                if name.lower() == CLIPPINGS_NAME:
                    src = os.path.join(d, name)
                    shutil.copy2(src, dest)
                    return f"USB drive {vol}"
    return None


class _MtpFile(ctypes.Structure):
    pass


_MtpFile._fields_ = [
    ("item_id", ctypes.c_uint32),
    ("parent_id", ctypes.c_uint32),
    ("storage_id", ctypes.c_uint32),
    ("filename", ctypes.c_char_p),
    ("filesize", ctypes.c_uint64),
    ("modificationdate", ctypes.c_long),
    ("filetype", ctypes.c_int),
    ("next", ctypes.POINTER(_MtpFile)),
]


class _MtpStorage(ctypes.Structure):
    pass


_MtpStorage._fields_ = [
    ("id", ctypes.c_uint32),
    ("StorageType", ctypes.c_uint16),
    ("FilesystemType", ctypes.c_uint16),
    ("AccessCapability", ctypes.c_uint16),
    ("MaxCapacity", ctypes.c_uint64),
    ("FreeSpaceInBytes", ctypes.c_uint64),
    ("FreeSpaceInObjects", ctypes.c_uint64),
    ("StorageDescription", ctypes.c_char_p),
    ("VolumeIdentifier", ctypes.c_char_p),
    ("next", ctypes.POINTER(_MtpStorage)),
    ("prev", ctypes.POINTER(_MtpStorage)),
]


class _MtpDevice(ctypes.Structure):
    # Prefix of LIBMTP_mtpdevice_struct — only read up to `storage`.
    _fields_ = [
        ("object_bitsize", ctypes.c_uint8),
        ("params", ctypes.c_void_p),
        ("usbinfo", ctypes.c_void_p),
        ("storage", ctypes.POINTER(_MtpStorage)),
    ]


class _MtpDeviceEntry(ctypes.Structure):
    _fields_ = [
        ("vendor", ctypes.c_char_p),
        ("vendor_id", ctypes.c_uint16),
        ("product", ctypes.c_char_p),
        ("product_id", ctypes.c_uint16),
        ("device_flags", ctypes.c_uint32),
    ]


class _MtpRawDevice(ctypes.Structure):
    _fields_ = [
        ("device_entry", _MtpDeviceEntry),
        ("bus_location", ctypes.c_uint32),
        ("devnum", ctypes.c_uint8),
    ]


def _load_libmtp():
    path = next((p for p in _LIBMTP_CANDIDATES if os.path.exists(p)), None)
    path = path or ctypes.util.find_library("mtp")
    if not path:
        raise KindleFetchError(
            "libmtp not found — install it with: brew install libmtp"
        )
    lib = ctypes.CDLL(path)
    lib.LIBMTP_Init.restype = None
    lib.LIBMTP_Detect_Raw_Devices.argtypes = [
        ctypes.POINTER(ctypes.POINTER(_MtpRawDevice)),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.LIBMTP_Detect_Raw_Devices.restype = ctypes.c_int
    # Uncached open is required: LIBMTP_Get_Files_And_Folders refuses to run
    # on a cached device (as opened by LIBMTP_Get_First_Device).
    lib.LIBMTP_Open_Raw_Device_Uncached.argtypes = [ctypes.POINTER(_MtpRawDevice)]
    lib.LIBMTP_Open_Raw_Device_Uncached.restype = ctypes.POINTER(_MtpDevice)
    lib.LIBMTP_Release_Device.argtypes = [ctypes.POINTER(_MtpDevice)]
    lib.LIBMTP_Release_Device.restype = None
    lib.LIBMTP_Get_Modelname.argtypes = [ctypes.POINTER(_MtpDevice)]
    lib.LIBMTP_Get_Modelname.restype = ctypes.c_char_p
    lib.LIBMTP_Get_Storage.argtypes = [ctypes.POINTER(_MtpDevice), ctypes.c_int]
    lib.LIBMTP_Get_Storage.restype = ctypes.c_int
    lib.LIBMTP_Get_Files_And_Folders.argtypes = [
        ctypes.POINTER(_MtpDevice),
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    lib.LIBMTP_Get_Files_And_Folders.restype = ctypes.POINTER(_MtpFile)
    lib.LIBMTP_Get_File_To_File.argtypes = [
        ctypes.POINTER(_MtpDevice),
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    lib.LIBMTP_Get_File_To_File.restype = ctypes.c_int
    return lib


def _walk(node):
    """Yield (item_id, name, filetype) from a LIBMTP_file_t linked list."""
    while node:
        f = node.contents
        name = (f.filename or b"").decode("utf-8", "replace")
        yield f.item_id, name, f.filetype
        node = f.next


def _find_clippings_id(lib, dev):
    """Search storage roots and their top-level folders. Returns (storage_id, file_id)."""
    if not dev.contents.storage:
        lib.LIBMTP_Get_Storage(dev, 0)
    storages = []
    s = dev.contents.storage
    while s:
        storages.append(s.contents.id)
        s = s.contents.next
    if not storages:
        raise KindleFetchError("Kindle reported no MTP storage — unlock its screen and retry.")

    for sid in storages:
        root = list(_walk(lib.LIBMTP_Get_Files_And_Folders(dev, sid, _PARENT_ROOT)))
        for item_id, name, _ft in root:
            if name.lower() == CLIPPINGS_NAME:
                return sid, item_id
        # Clippings live in documents/; check it first, then other folders.
        folders = [x for x in root if x[2] == _FILETYPE_FOLDER]
        folders.sort(key=lambda x: x[1].lower() != "documents")
        for folder_id, _name, _ft in folders[:_MAX_FOLDER_SCANS]:
            kids = _walk(lib.LIBMTP_Get_Files_And_Folders(dev, sid, folder_id))
            for item_id, name, _kft in kids:
                if name.lower() == CLIPPINGS_NAME:
                    return sid, item_id
    return None, None


def _fetch_mtp(dest: str):
    """Newer Kindles speak MTP; one libmtp session does list + fetch."""
    lib = _load_libmtp()
    lib.LIBMTP_Init()
    raw = ctypes.POINTER(_MtpRawDevice)()
    num = ctypes.c_int(0)
    err = lib.LIBMTP_Detect_Raw_Devices(ctypes.byref(raw), ctypes.byref(num))
    if err != 0 or num.value == 0:
        return None
    dev = lib.LIBMTP_Open_Raw_Device_Uncached(raw)
    if not dev:
        raise KindleFetchError(
            "Found the Kindle on USB but couldn't open an MTP session. "
            "Unplug/replug it and try again."
        )
    try:
        model = lib.LIBMTP_Get_Modelname(dev)
        model = model.decode("utf-8", "replace") if model else ""
        if model and not KINDLE_DEVICE_RE.search(model):
            raise KindleFetchError(
                f"An MTP device is connected but it doesn't look like a Kindle: {model}"
            )

        _sid, file_id = _find_clippings_id(lib, dev)
        if file_id is None:
            raise KindleFetchError(
                "Connected to the Kindle over MTP but found no 'My Clippings.txt' "
                "in the top two folder levels. Unlock the Kindle screen, then "
                "unplug/replug and try again."
            )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp_path = tmp.name
        try:
            ret = lib.LIBMTP_Get_File_To_File(
                dev, file_id, tmp_path.encode("utf-8"), None, None
            )
            if ret != 0 or not os.path.getsize(tmp_path):
                raise KindleFetchError(
                    f"libmtp failed to copy 'My Clippings.txt' (id {file_id})"
                )
            shutil.move(tmp_path, dest)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        return f"MTP ({model or 'unknown device'})"
    finally:
        lib.LIBMTP_Release_Device(dev)


def fetch_clippings(dest: str) -> str:
    """Copy My Clippings.txt from a connected Kindle to `dest`.

    Returns a description of the transport used. Raises KindleFetchError if no
    Kindle is found or the transfer fails. Never leaves a partial file at dest.
    """
    via = _fetch_mass_storage(dest)
    if via:
        return via
    via = _fetch_mtp(dest)
    if via:
        return via
    raise KindleFetchError(
        "No Kindle found over USB or MTP. The Scribe drops its USB connection "
        "after any MTP session ends (including a failed run, or quitting "
        "OpenMTP) — unplug it, plug it back in, and run this again without "
        "opening any other MTP app in between."
    )
