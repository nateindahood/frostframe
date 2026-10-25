"""Message layouts for the cooler screen's USB protocol (see docs/PROTOCOL.md).

Field numbers and types were determined for interoperability; names are frostframe's own.
Format of each spec: {field_number: (name, kind, extra)}, see wire.py.
"""

# ----------------------------------------------------------------- basics ---

HEADER = {1: ("version", "uint", None), 2: ("track_id", "uint", None), 3: ("payload_crc32", "uint", None)}
DUMMY = {1: ("dummy", "string", None)}
PING = {1: ("payload", "bytes", None)}
AUTH_REQ = {1: ("key", "uint", None)}
AUTH = {1: ("auth", "string", None)}

ERROR_CODES = {"Success": 0, "Fail": 1, "BodyCaseNotSupported": 2}
ERROR = {1: ("code", "enum", ERROR_CODES), 2: ("why", "string", None)}

DEVICE_INFO = {
    1: ("os_name", "string", None), 2: ("os_version", "string", None),
    3: ("firmware_version", "string", None), 4: ("product_name", "string", None),
    5: ("app_version", "string", None), 8: ("serial_number", "string", None),
    9: ("serial_number_locked", "bool", None), 10: ("chip_id", "string", None),
}

SIZE = {1: ("width", "uint", None), 2: ("height", "uint", None)}
OUTPUT_SIZE = {1: ("width", "uint", None), 2: ("height", "uint", None),
               3: ("rotation", "uint", None), 4: ("mirror", "bool", None)}
SYS_CONFIG = {
    1: ("product_name", "string", None),
    10: ("decoder", "msg", SIZE),
    11: ("scaler", "msg", SIZE),
    12: ("output", "msg", OUTPUT_SIZE),
}

STORAGE_TYPES = {"EMMC": 0, "TFCard": 1}
STORAGE_STATUS = {1: ("storage_type", "enum", STORAGE_TYPES), 2: ("available_bytes", "uint", None),
                  3: ("abnormal", "bool", None), 4: ("error_message", "string", None)}

# ----------------------------------------------------------------- files ----

MEDIA_FILE = {1: ("path", "string", None), 2: ("ext", "string", None),
              3: ("size", "uint", None), 4: ("read_only", "bool", None)}
FILE_LIST = {1: ("media", "*msg", MEDIA_FILE), 2: ("presets", "*msg", MEDIA_FILE)}

FILE_BEGIN = {1: ("name", "bytes", None), 2: ("size", "uint", None)}
FILE_DATA = {1: ("data", "bytes", None)}
FILE_END = {1: ("file_type", "bytes", None), 2: ("crc", "uint", None)}
FILE_STATUS_CODES = {"OK": 0, "SpaceNotEnough": 1, "FileError": 2, "CRCFail": 3}
FILE_STATUS = {1: ("status", "enum", FILE_STATUS_CODES)}
FILE_REMOVE = {1: ("name", "string", None), 2: ("file_type", "string", None)}

# Header prepended to uploaded media files (uint32 LE length + this message + payload)
ORIGINAL_TYPES = {"Unknown": 0, "JPEG": 1, "PNG": 2, "GIF": 3, "MP4": 4}
PAYLOAD_FORMATS = {"Unknown": 0, "H264Raw": 1, "MJPEGRaw": 2}
MEDIA_HEADER = {
    1: ("magic", "uint", None), 2: ("description", "string", None),
    3: ("original_type", "enum", ORIGINAL_TYPES), 4: ("payload_format", "enum", PAYLOAD_FORMATS),
    5: ("frame_rate", "uint", None), 6: ("width", "uint", None), 7: ("height", "uint", None),
    8: ("frame_count", "uint", None),
}
MEDIA_MAGIC = 1297631300

# ----------------------------------------------------------- user config ----

MEDIA_MODES = {"Single": 0, "Dual": 1, "Kaleidoscope": 2}
LOOP_MODES = {"Single": 0, "MediaAll": 1, "MediaRand": 2, "PresetAll": 3, "PresetRand": 4}
SIDES = {"Left": 0, "Right": 1}
FILTER_MODES = {"Single": 0, "Dual": 1}
LIGHT_EFFECTS = {"BatteryFlow": 0, "Flow": 1, "Neon": 2, "Solid": 3, "Breath": 4, "Indicator": 5, "Off": 6}
LIGHT_CONTROL = {"I2C": 0, "ARGBSync": 1}

RGB = {1: ("r", "uint", None), 2: ("g", "uint", None), 3: ("b", "uint", None)}
USER_CONFIG = {
    1: ("poweron", "msg", {1: ("media_file", "string", None)}),
    2: ("standby", "msg", {1: ("enable", "bool", None), 2: ("media_file", "string", None)}),
    3: ("work", "msg", {
        1: ("media_mode", "enum", MEDIA_MODES), 2: ("loop_mode", "enum", LOOP_MODES),
        3: ("single_file", "string", None), 4: ("left_file", "string", None),
        5: ("right_file", "string", None), 6: ("kaleidoscope_file", "string", None),
        7: ("kaleidoscope_source", "enum", SIDES),
    }),
    4: ("filter", "msg", {
        1: ("enable", "bool", None), 2: ("file", "string", None), 3: ("alpha", "uint", None),
        4: ("left_file", "string", None), 5: ("right_file", "string", None),
        6: ("left_alpha", "uint", None), 7: ("right_alpha", "uint", None),
        8: ("mode", "enum", FILTER_MODES),
    }),
    5: ("display", "msg", {
        1: ("backlight", "bool", None), 2: ("brightness", "uint", None), 3: ("mirror", "bool", None),
        4: ("ui_rotation", "uint", None), 5: ("media_rotation", "uint", None),
    }),
    6: ("light", "msg", {
        1: ("enable", "bool", None), 2: ("brightness", "uint", None), 3: ("effect", "enum", LIGHT_EFFECTS),
        4: ("color", "msg", RGB), 5: ("speed", "uint", None), 10: ("control_mode", "enum", LIGHT_CONTROL),
    }),
}

# ------------------------------------------------------- on-screen labels ---

BACKGROUNDS = {"None": 0, "Color": 1, "GradientVertical": 2, "GradientHorizontal": 3}
ALIGN = {"Auto": 0, "Left": 1, "Center": 2, "Right": 3}
LABEL = {
    1: ("id", "uint", None), 2: ("line", "uint", None), 3: ("order", "uint", None),
    4: ("gap_left", "sint", None), 5: ("background", "enum", BACKGROUNDS),
    6: ("background_color", "uint", None), 7: ("gradient_color", "uint", None),
    8: ("font", "string", None), 9: ("size", "uint", None), 10: ("color", "uint", None),
    11: ("text", "string", None),
}
LABEL_GROUP = {
    1: ("id", "uint", None), 2: ("x", "uint", None), 3: ("y", "uint", None),
    4: ("width", "uint", None), 5: ("height", "uint", None), 6: ("align", "enum", ALIGN),
    7: ("line_gap", "sint", None), 8: ("labels", "*msg", LABEL),
}
RUN_CONFIG = {1: ("groups", "*msg", LABEL_GROUP)}
LABEL_TEXT = {1: ("id", "uint", None), 2: ("text", "string", None)}
LABEL_UPDATE = {1: ("groups", "*msg", {1: ("id", "uint", None), 2: ("labels", "*msg", LABEL_TEXT)})}

# ------------------------------------------------------------------ cooler ---

FAN_MODES = {"Smart": 0, "Fixed": 1}
COOLER_CONFIG = {
    1: ("fan", "msg", {
        1: ("mode", "enum", FAN_MODES),
        2: ("curve", "*msg", {1: ("temperature", "uint", None), 2: ("power", "uint", None)}),
        3: ("fixed_power", "uint", None),
    }),
    2: ("pump", "msg", {1: ("speed_percent", "uint", None)}),
}
COOLER_STATUS = {1: ("fan_rpm", "uint", None), 2: ("pump_rpm", "uint", None)}

# ------------------------------------------------------------- system ctl ---

# Not exposed in the CLI: on the tested firmware, "Reboot" restarts the screen into its Rockchip
# flashing (loader) mode, where it stays until the PC's power supply is switched off and on.
SYS_COMMANDS = {"None": 0, "InstallApp": 1, "Reboot": 2, "RestoreConfig": 3, "RestoreDevice": 4,
                "Console": 5, "Reset": 6}
SYS_EXEC = {1: ("cmd", "enum", SYS_COMMANDS), 2: ("params", "string", None)}

# ---------------------------------------------------------------- packets ---
# Request: header (1) + one body field. Response: header (1) + error (2) + one body field.

REQUESTS = {
    "ping": (10, PING),
    "get_device_info": (100, DUMMY),
    "get_device_auth": (101, AUTH_REQ),
    "get_sys_config": (102, DUMMY),
    "get_file_list": (103, DUMMY),
    "get_user_config": (104, DUMMY),
    "get_run_config": (105, DUMMY),
    "get_cooler_config": (106, DUMMY),
    "get_cooler_status": (107, DUMMY),
    "get_storage_status": (110, DUMMY),
    "user_config": (200, USER_CONFIG),
    "run_config": (201, RUN_CONFIG),
    "cooler_config": (202, COOLER_CONFIG),
    "label_update": (300, LABEL_UPDATE),
    "label_group_create": (301, LABEL_GROUP),
    "label_group_destroy": (302, LABEL_GROUP),
    "file_begin": (400, FILE_BEGIN),
    "file_data": (401, FILE_DATA),
    "file_end": (402, FILE_END),
    "file_remove": (403, FILE_REMOVE),
    "sys_exec": (450, SYS_EXEC),
}

RESPONSES = {
    10: ("pong", PING),
    500: ("device_info", DEVICE_INFO),
    501: ("device_auth", AUTH),
    502: ("sys_config", SYS_CONFIG),
    503: ("file_list", FILE_LIST),
    504: ("user_config", USER_CONFIG),
    505: ("run_config", RUN_CONFIG),
    506: ("cooler_config", COOLER_CONFIG),
    507: ("cooler_status", COOLER_STATUS),
    510: ("storage_status", STORAGE_STATUS),
    600: ("ok", DUMMY),
    800: ("file_begin_status", FILE_STATUS),
    801: ("file_data_status", FILE_STATUS),
    802: ("file_end_status", FILE_STATUS),
    850: ("sys_exec_result", {1: ("status", "uint", None), 2: ("message", "string", None)}),
    822: ("app_exception", {1: ("type", "uint", None), 2: ("error_code", "sint", None),
                            3: ("source", "string", None), 4: ("message", "string", None)}),
}

RESPONSE_SPEC = {1: ("header", "msg", HEADER), 2: ("error", "msg", ERROR)}
RESPONSE_SPEC.update({num: (name, "msg", spec) for num, (name, spec) in RESPONSES.items()})

FRAME_MAGIC = b"TRYX"  # 4-byte frame marker used by the device firmware
