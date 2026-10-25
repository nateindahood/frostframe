# Cooler screen USB protocol

Observed on: AIO cooler screen with USB ID `391a:1011` (product "PANO", manufacturer "RK"),
device firmware `v2.0.6.20260713`, screen app `v2.0.4.20260804`, display 2240×1080.
Determined for interoperability by observing the manufacturer's Windows software talking to the
device (message framing and field numbers), without modifying the device. frostframe's
implementation of these messages is in `src/frostframe/protocol.py`.

## Transport

- One USB interface, class 07/01/02 (printer, bidirectional). On Linux: `usblp` → `/dev/usb/lpN`.
  Plain `read()`/`write()` on that node carries the protocol. No ioctls needed.
- Every message in both directions is framed as:

  ```
  "TRYX" (4 bytes ASCII) | length (uint32 little-endian) | protobuf payload (length bytes)
  ```

- Host → device payload: `ReqPackagePb`. Device → host: `RspPackagePb`.
- `ReqPackagePb` = `header` (field 1) + exactly one body field (oneof). `header` is
  `{version=1, track_id, payload_crc32}`; the vendor sends `version: 1` on some requests and an
  empty header on others; `track_id` is only set for file transfers; `payload_crc32` is never set.
- `RspPackagePb` = `header` + `error` (field 2; empty = OK) + one body field.
  Unknown/unsupported requests get `error {code: BodyCaseNotSupported}`.
- Reads return one or more frames; buffer and split on the length prefix.

## Request body fields (host → device)

| Field # | Name | Payload | Notes |
|---|---|---|---|
| 10 | ping | `{payload: bytes}` | device answers `pong {payload: "Hey!"}` |
| 100 | get_device_info | `DummyMsg{dummy:"NA"}` | → `device_info` (500) |
| 101 | get_device_auth | `{key: 1}` | → `device_auth {auth: "ok"}` (no secret involved) |
| 102 | get_sys_config | Dummy | → `sys_config` (502): product name, decoder/output sizes (2240×1080) |
| 103 | get_file_list | Dummy | → `file_list` (503): `media_file_list` + `preset_file_list` |
| 104 | get_user_config | Dummy | → `user_config` (504) |
| 105 | get_run_config | Dummy | → `run_config` (505) |
| 106 | get_cooler_config | Dummy | → `cooler_config` (506) |
| 107 | get_cooler_status | Dummy | → `cooler_status` (507) `{fans_speed, pump_speed}` |
| 110 | get_storage_status | Dummy | → `storage_status` (510) |
| 200 | user_config | `UserConfigPb` | media selection, brightness, power-on/standby clips, filter, RGB light |
| 201 | run_config | `RunConfigPb` | on-screen text label groups (layout, font, size, color) |
| 202 | cooler_config | `CoolerConfigPb` | fan mode (smart curve / fixed %) + pump speed % |
| 300 | batch_group_label_update | label texts by group/label id | sent ~1/s for live values |
| 301/302 | label_group_create/destroy | `LabelGroupPb` | |
| 400 | file_transmit_begin | `{file_name: bytes, file_size}` | |
| 401 | file_transmit_data | `{file_data: bytes}` | 256 KiB chunks, one response each |
| 402 | file_transmit_end | `{file_type: "media", CRC}` | CRC = **CRC-32C** of the whole file |
| 403 | file_remove | | |
| 407–409 | media_push_begin/data/end | `{payload_format, frame_rate, width, height}` / data | live streaming to the screen (not yet tested) |
| 420 | host_ctl_set_volume | `{volume}` | vendor sends it; this device answers BodyCaseNotSupported |
| 450 | sys_ctl_exec | `{cmd: "Reboot" / "InstallApp", params}` | **don't use**: on firmware v2.0.6, `Reboot` restarts the screen into Rockchip loader mode (USB `2207:350a`) and it stays there until a full power cycle (PSU off) |

(Fields 404–406 are for the vendor's encrypted store content and pulling files; 451, 900, 999 are
serial-number/factory/debug functions. Out of scope.)

## Handshake seen from the vendor app

```
-> get_device_info {dummy:"NA"}       <- device_info {os_name:"Linux" os_version:"kernel 5.1" firmware_version product_name:"PANO" app_version serial_number serial_number_locked chip_id}
-> get_sys_config                     <- sys_config {product_name vdec_param{2240x1080} vpss_param vo_param}
-> get_device_auth {key:1}            <- device_auth {auth:"ok"}
-> get_file_list                      <- file_list {...}
-> run_config {...}                   <- dummy_msg {dummy:"ok"}
then every second: batch_group_label_update + get_cooler_status
```

## Uploading media

Files on the device live under `/userdata/...`; names carry the format as an extension, e.g.
`default_01.mp4.h264_2240x1080`, `2026-10-23_17-11-40-395.gif.h264_1120x1080`.

Uploaded file layout:

```
uint32 LE header_length | MediaHeaderPb (header_length bytes) | raw H.264 Annex-B stream
```

`MediaHeaderPb` as sent: `magic: 1297631300, description: "Tryx media header v1, fps=30, size=1120x1080",
original_media_type: GIF (JPEG/PNG/GIF/MP4), payload_format: H264Raw (or MJPEGRaw), frame_rate: 30,
width, height, total_frame_count`.

The H.264 stream observed: Main profile, Annex-B start codes, access-unit delimiters, 30 fps.
Full-screen media is 2240×1080; half-screen (dual mode, one side) 1120×1080.

Sequence: `file_transmit_begin` → N × `file_transmit_data` (256 KiB, wait for each
`file_transmit_data_status`) → `file_transmit_end {file_type:"media", CRC: crc32c(file)}`.
7.9 MB took ~1.3 s.

## Selecting media / display settings (`user_config`)

```
poweron_config { media_file }                 # clip shown at power-on
standby_config { enable, media_file }
work_config {
  media_mode: Single | Dual | Kaleidoscope
  loop_mode: Single | MediaAll | MediaRand | PresetAll | PresetRand
  single_mode_media_file, dual_mode_left_media_file, dual_mode_right_media_file,
  kaleidoscope_media_file, kaleidoscope_source
}
filter_config { enable, filter_file, alpha, ... }   # overlay effects (rain/smoke/ripple clips)
display_config { backlight_enable, backlight_brightness (0-100), mirror, ui_rotation, media_rotation }
light_config { enable, brightness, effect, color{r,g,b}, speed, control_mode }   # RGB, untested
```

Media files are referenced by bare file name (e.g. `default_04.mp4.h264_2240x1080`).

## On-screen text (`run_config`, `batch_group_label_update`)

`run_config.label_groups[]`: `group_id, group_x, group_y, group_width, group_height, text_align
(Left/...), line_gap, labels[] {label_id, line, gap_left, text_font, text_size, text_color (0xRRGGBB
as integer; vendor "white" = 0xDCDCDC), text}`, optional background/gradient colors.
Live values are pushed with `batch_group_label_update { label_groups { group_id label_texts {label_id text} } }`.
Fonts seen: `roboto-regular`, `RubikGlitch-Regular` (built into the device).

## Cooler (fan/pump)

`cooler_config { fan_config { mode: SmartMode|FixMode, fun_power_level[] {temperature, power}, fix_power }, pump_config { speed_percent } }`.
`cooler_status { fans_speed, pump_speed }` came back empty (= 0/0) on the test system, consistent
with the fans/pump being wired to the motherboard rather than the cooler. The vendor app's
"High" preset sends `FixMode, fix_power: 80`; device answers OK.
