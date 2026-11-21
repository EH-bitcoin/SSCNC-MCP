"""Installation paths and the observed layout for the pinned MF 7.2.5.2 build."""

import os
from pathlib import Path

INSTALL_DIRECTORY = str(Path(os.environ.get(
    "SSCNC_INSTALL_DIR", r"C:\Program Files (x86)\Nanjing Swansoft\SSCNC"
)).expanduser().resolve())
TARGET_PATH = str(Path(INSTALL_DIRECTORY) / "FANUC" / "FANUC0iMF" / "sscnc.exe")
TARGET_SHA256 = "9e27c17d3d1f75af7db51f03829254f3744f2dc6514e25dcca84f9be50c28f10"
WORKSPACE = str(Path(os.environ.get(
    "SSCNC_WORKSPACE", Path(__file__).resolve().parent
)).expanduser().resolve())
MAIN_CLASS = "ClassName_Swansoft_CNC_Simulation"
MEM_MODE = 2
START_ID = 9518
HOLD_ID = 9508
SAVE_AS_ID = 32827
VIEWS = {"front": 32866, "right": 32867, "top": 32869,
         "zoom_in": 32834, "zoom_out": 32835}
# Distinct keyboard buttons and display labels from the observed MF window.
CONTROLLER_FINGERPRINT = frozenset(
    [("Button", control_id) for control_id in (7001, 7012, 7024, 7030, 7044, 7049, 7050, 7056)] +
    [("Static", control_id) for control_id in (7101, 7105, 7110, 7113)]
)
GLOBALS = {
    "document": (0x5CB178, "I"), "cycle": (0x5C6924, "I"),
    "worker": (0x5CC83E, "B"), "mode": (0x5C691C, "I"),
    "emergency": (0x5CB458, "I"), "power": (0x5CC7F0, "I"),
    "current_mcode": (0x5CB460, "i"), "m30_count": (0x5CC7C4, "I"),
    "end_count": (0x5CE190, "I"),
    "x": (0x5CC998, "d"), "y": (0x5CC9A0, "d"), "z": (0x5CC9A8, "d"),
}
DOC_FIELDS = {"index": (0xD8, "i"), "source_line": (0xE0, "i"),
              "descriptor_array": (0x3F0C, "I"), "descriptor_count": (0x3F10, "i")}
