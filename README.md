# AceMate OTA

Custom firmware update tool for the AceMate tennis robot (current available version is v1.4.3).

The official app frequently fails to complete firmware updates. This tool reimplements the OTA protocol as a standalone Python CLI, giving you direct control over each step.

## How it works

The AceMate robot runs a Linux system (RK-based) with an nRF52832 BLE chip. Firmware updates use a hybrid BLE + WiFi approach:

1. **BLE command** tells the robot to open a WiFi hotspot
2. **Phone/PC connects** to the robot's open WiFi network (IP: `10.42.0.1`)
3. **HTTP upload** sends the firmware file to `http://10.42.0.1:5000/upload`
4. **HTTP trigger** starts the flash via `http://10.42.0.1:5000/trigger_update`
5. **MCU progress** is reported back over BLE

The tool supports the full BLE-driven flow or a simpler direct-upload mode if you can get on the robot's WiFi manually.

## Setup

```bash
pip install -r requirements.txt
```

## Firmware files

Firmware binaries are stored in the `firmware/` directory. You can obtain them in two ways:

**From the cloud API:**

```bash
python acemate_ota.py --download --output-dir firmware/
```

This calls `https://api.acematetennis.com.cn/v1/app/configs` to get the latest firmware URL (hosted on Alibaba Cloud OSS) and downloads it.

**From the APK:**

The official APK bundles firmware in its assets. After decompiling with jadx or apktool:

```
resources/assets/ota_package_acemate_<version>.tar
```

Copy it to `firmware/`.

## Usage

### Query available firmware

```bash
python acemate_ota.py --query
```

Returns the firmware URL, filename, and version from the cloud API.

### Direct upload (skip BLE)

If you can connect to the robot's WiFi hotspot manually (e.g. via the app's partial flow, or if the hotspot is already open):

```bash
# Connect to the robot's WiFi first, then:
python acemate_ota.py --upload-only firmware/ota_package_acemate_1.4.3.tar
```

You can verify connectivity before uploading:

```bash
python acemate_ota.py --upload-only firmware/ota_package_acemate_1.4.3.tar --check
```

### Full BLE flow

Handles everything: BLE scan, hotspot open command, WiFi connect prompt, upload, and trigger.

```bash
python acemate_ota.py --update firmware/ota_package_acemate_1.4.3.tar
```

Options:

```
--wifi-band {5G,2.4G}   WiFi band for hotspot (default: 5G)
-v, --verbose            Debug logging
```

## Protocol details

### BLE

- **Service:** Nordic UART (NUS)
- **TX (write):** `6e400002-b5a3-f393-e0a9-e50e24dcca9e`
- **RX (notify):** `6e400003-b5a3-f393-e0a9-e50e24dcca9e`
- **Message format:** JSON + `\r\n` + `+==ACE==+` delimiter
- **Min send interval:** 200ms between writes

### WiFi AP open command

```json
{"type":"ap","data":{"purpose":"ota","wifiBand":"5G","wifi_channels":[149,153]}}
```

Device responds with:

```json
{"type":"rsp_ap","data":{"purpose":"ota","ssid":"AceMate-XXXX"}}
```

### HTTP endpoints (on `10.42.0.1:5000`)

| Endpoint          | Method | Body                         | Description              |
|-------------------|--------|------------------------------|--------------------------|
| `/upload`         | POST   | Multipart, field=`file`      | Upload firmware (`.gz`)  |
| `/trigger_update` | POST   | Empty                        | Start flashing           |

The upload sends the file with `.gz` appended to the filename in the multipart form.

### MCU progress (via BLE notify)

```json
{"type":"mcu_ota_result","data":{"Progress":42.5}}
```

## Tests

```bash
pytest test_acemate_ota.py -v
```

## Troubleshooting

**Upload times out or connection refused:**
Make sure you are connected to the robot's WiFi hotspot, not your regular network. The robot's server is only reachable at `10.42.0.1:5000`.

**BLE scan finds no devices:**
Ensure Bluetooth is enabled and the robot is powered on. On Linux you may need to run with `sudo` or add your user to the `bluetooth` group.

**5G band timeout:**
Some environments have poor 5GHz support. Use `--wifi-band 2.4G` or let the tool auto-fallback (it retries on 2.4G after a 60s timeout on 5G).
