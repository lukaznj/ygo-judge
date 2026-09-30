"""ctypes binding to libocgcore, EDOPro's rules engine (vendor/ygopro-core/ocgapi.h)."""

import ctypes as C
import os
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Locations, positions and flags the harness needs (ocgapi_constants.h).
LOCATION_DECK = 0x01
LOCATION_HAND = 0x02
LOCATION_MZONE = 0x04
LOCATION_SZONE = 0x08
LOCATION_GRAVE = 0x10
LOCATION_REMOVED = 0x20
LOCATION_EXTRA = 0x40
LOCATION_OVERLAY = 0x80
POS_FACEUP_ATTACK = 0x1
POS_FACEDOWN_ATTACK = 0x2
POS_FACEUP_DEFENSE = 0x4
POS_FACEDOWN_DEFENSE = 0x8
POS_FACEUP = 0x5
POS_FACEDOWN = 0xA
DUEL_ATTACK_FIRST_TURN = 0x2
DUEL_PSEUDO_SHUFFLE = 0x10
DUEL_MODE_MR5 = 0x800 | 0x2000 | 0x4000 | 0x8000 | 0x20000

STATUS_END, STATUS_AWAITING, STATUS_CONTINUE = 0, 1, 2
LOG_ERROR, LOG_FROM_SCRIPT = 0, 1

QUERY_CODE = 0x1
QUERY_POSITION = 0x2
QUERY_ALIAS = 0x4
QUERY_TYPE = 0x8
QUERY_LEVEL = 0x10
QUERY_RANK = 0x20
QUERY_ATTRIBUTE = 0x40
QUERY_RACE = 0x80
QUERY_ATTACK = 0x100
QUERY_DEFENSE = 0x200
QUERY_BASE_ATTACK = 0x400
QUERY_BASE_DEFENSE = 0x800
QUERY_REASON = 0x1000
QUERY_REASON_CARD = 0x2000
QUERY_EQUIP_CARD = 0x4000
QUERY_TARGET_CARD = 0x8000
QUERY_OVERLAY_CARD = 0x10000
QUERY_COUNTERS = 0x20000
QUERY_OWNER = 0x40000
QUERY_STATUS = 0x80000
QUERY_IS_PUBLIC = 0x100000
QUERY_LSCALE = 0x200000
QUERY_RSCALE = 0x400000
QUERY_LINK = 0x800000
QUERY_IS_HIDDEN = 0x1000000
QUERY_COVER = 0x2000000
QUERY_END = 0x80000000


class CardData(C.Structure):
    _fields_ = [
        ("code", C.c_uint32),
        ("alias", C.c_uint32),
        ("setcodes", C.POINTER(C.c_uint16)),
        ("type", C.c_uint32),
        ("level", C.c_uint32),
        ("attribute", C.c_uint32),
        ("race", C.c_uint64),
        ("attack", C.c_int32),
        ("defense", C.c_int32),
        ("lscale", C.c_uint32),
        ("rscale", C.c_uint32),
        ("link_marker", C.c_uint32),
    ]


class Player(C.Structure):
    _fields_ = [("startingLP", C.c_uint32), ("startingDrawCount", C.c_uint32), ("drawCountPerTurn", C.c_uint32)]


DataReader = C.CFUNCTYPE(None, C.c_void_p, C.c_uint32, C.POINTER(CardData))
DataReaderDone = C.CFUNCTYPE(None, C.c_void_p, C.POINTER(CardData))
ScriptReader = C.CFUNCTYPE(C.c_int, C.c_void_p, C.c_void_p, C.c_char_p)
LogHandler = C.CFUNCTYPE(None, C.c_void_p, C.c_char_p, C.c_int)


class DuelOptions(C.Structure):
    _fields_ = [
        ("seed", C.c_uint64 * 4),
        ("flags", C.c_uint64),
        ("team1", Player),
        ("team2", Player),
        ("cardReader", DataReader),
        ("payload1", C.c_void_p),
        ("scriptReader", ScriptReader),
        ("payload2", C.c_void_p),
        ("logHandler", LogHandler),
        ("payload3", C.c_void_p),
        ("cardReaderDone", DataReaderDone),
        ("payload4", C.c_void_p),
        ("enableUnsafeLibraries", C.c_uint8),
    ]


class QueryInfo(C.Structure):
    _fields_ = [
        ("flags", C.c_uint32),
        ("con", C.c_uint8),
        ("loc", C.c_uint32),
        ("seq", C.c_uint32),
        ("overlay_seq", C.c_uint32),
    ]


def _load_library() -> C.CDLL:
    default = ROOT / "build" / ("libocgcore.dylib" if sys.platform == "darwin" else "libocgcore.so")
    lib = C.CDLL(os.environ.get("OCGCORE_LIB", str(default)))
    lib.OCG_CreateDuel.argtypes = [C.POINTER(C.c_void_p), C.POINTER(DuelOptions)]
    lib.OCG_DestroyDuel.argtypes = [C.c_void_p]
    lib.OCG_StartDuel.argtypes = [C.c_void_p]
    lib.OCG_DuelProcess.argtypes = [C.c_void_p]
    lib.OCG_DuelGetMessage.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
    lib.OCG_DuelGetMessage.restype = C.c_void_p
    lib.OCG_DuelSetResponse.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32]
    lib.OCG_LoadScript.argtypes = [C.c_void_p, C.c_char_p, C.c_uint32, C.c_char_p]
    for fn in ("OCG_DuelQuery", "OCG_DuelQueryLocation"):
        getattr(lib, fn).argtypes = [C.c_void_p, C.POINTER(C.c_uint32), C.POINTER(QueryInfo)]
        getattr(lib, fn).restype = C.c_void_p
    lib.OCG_DuelQueryField.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
    lib.OCG_DuelQueryField.restype = C.c_void_p
    return lib


lib = _load_library()


class EngineError(Exception):
    pass


class Engine:
    """One duel inside libocgcore.

    `read_card(code, CardData)` fills card data, `read_script(name)` returns a script's source
    (or None) and `log(text, type)` receives engine and Lua messages.
    """

    def __init__(
        self,
        seed: int,
        flags: int,
        read_card: Callable[[int, CardData], None],
        read_script: Callable[[str], bytes | None],
        log: Callable[[str, int], None],
    ):
        self._read_script = read_script
        # ctypes callbacks must stay referenced for the duel's lifetime.
        self._cb_card = DataReader(lambda _p, code, data: read_card(code, data.contents))
        self._cb_done = DataReaderDone(lambda _p, _d: None)
        self._cb_script = ScriptReader(self._on_script)
        self._cb_log = LogHandler(lambda _p, text, kind: log(text.decode(errors="replace"), kind))
        opts = DuelOptions()
        for i in range(4):  # splitmix64 of the seed, so every seed gives a valid (non-zero) state
            x = (seed + (i + 1) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
            x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
            x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
            opts.seed[i] = (x ^ (x >> 31)) or 1
        opts.flags = flags
        opts.team1 = opts.team2 = Player(8000, 0, 0)
        opts.cardReader = self._cb_card
        opts.scriptReader = self._cb_script
        opts.logHandler = self._cb_log
        opts.cardReaderDone = self._cb_done
        opts.enableUnsafeLibraries = 0  # no io/os libraries inside card scripts
        self.handle = C.c_void_p()
        status = lib.OCG_CreateDuel(C.byref(self.handle), C.byref(opts))
        if status != 0:
            raise EngineError(f"OCG_CreateDuel failed with status {status}")

    def _on_script(self, _payload, duel, name: bytes) -> int:
        source = self._read_script(name.decode())
        if source is None:
            return 0
        return lib.OCG_LoadScript(duel, source, len(source), name)

    def load_script(self, source: bytes, name: str) -> bool:
        return lib.OCG_LoadScript(self.handle, source, len(source), name.encode()) != 0

    def start(self) -> None:
        lib.OCG_StartDuel(self.handle)

    def process(self) -> int:
        return lib.OCG_DuelProcess(self.handle)

    def messages(self) -> bytes:
        size = C.c_uint32()
        ptr = lib.OCG_DuelGetMessage(self.handle, C.byref(size))
        return C.string_at(ptr, size.value) if size.value else b""

    def respond(self, response: bytes) -> None:
        lib.OCG_DuelSetResponse(self.handle, response, len(response))

    def query(self, player: int, location: int, sequence: int, flags: int, overlay_sequence: int = 0) -> bytes:
        size = C.c_uint32()
        info = QueryInfo(flags, player, location, sequence, overlay_sequence)
        ptr = lib.OCG_DuelQuery(self.handle, C.byref(size), C.byref(info))
        return C.string_at(ptr, size.value) if ptr and size.value else b""

    def query_location(self, player: int, location: int, flags: int) -> bytes:
        size = C.c_uint32()
        info = QueryInfo(flags, player, location, 0, 0)
        ptr = lib.OCG_DuelQueryLocation(self.handle, C.byref(size), C.byref(info))
        return C.string_at(ptr, size.value) if ptr and size.value else b""

    def query_field(self) -> bytes:
        size = C.c_uint32()
        ptr = lib.OCG_DuelQueryField(self.handle, C.byref(size))
        return C.string_at(ptr, size.value) if ptr and size.value else b""

    def close(self) -> None:
        if self.handle:
            lib.OCG_DestroyDuel(self.handle)
            self.handle = C.c_void_p()

    def __del__(self):
        self.close()
