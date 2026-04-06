#!/usr/bin/env python3
"""
AceMate Tennis Robot - Firmware Update Tool

Reverse-engineered from the AceMate Android app v1.4.3.

Architecture:
  1. Fetch firmware info from cloud API (or use local file)
  2. Connect to robot via BLE and send WiFi AP open command
  3. Connect to robot's WiFi hotspot
  4. Upload firmware via HTTP POST to robot's local server
  5. Trigger firmware update
  6. Monitor MCU progress via BLE notifications

Usage:
  # Step 1: Query the cloud for available firmware
  python acemate_ota.py --query

  # Step 2: Download firmware from cloud
  python acemate_ota.py --download

  # Step 3: Full BLE+WiFi update flow
  python acemate_ota.py --update firmware.tar

  # Step 3 (alt): If you're already connected to robot's WiFi hotspot,
  #               skip BLE and just upload + trigger
  python acemate_ota.py --upload-only firmware.tar

  # Use bundled firmware from APK assets
  python acemate_ota.py --upload-only /path/to/ota_package_acemate_1.4.3.tar
"""

import argparse
import asyncio
import gzip
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path

import requests
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

# ---------------------------------------------------------------------------
# Constants (from decompiled app)
# ---------------------------------------------------------------------------

# Cloud API
API_BASE_URL = "https://api.acematetennis.com.cn"
API_INIT = "/v1/app/init"
API_CONFIGS = "/v1/app/configs"

# Robot local server (when connected to its WiFi hotspot)
RK_BASE_URL = "http://10.42.0.1:5000"
RK_OTA_UPLOAD = "/upload"
RK_TRIGGER_UPDATE = "/trigger_update"

# BLE UUIDs (Nordic UART Service on nRF52832)
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # Write (app → device)
NUS_RX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # Notify (device → app)

# BLE protocol
BLE_MSG_DELIMITER = "+==ACE==+"
BLE_SEND_INTERVAL = 0.2  # 200ms between BLE writes

# Upload
UPLOAD_MAX_RETRIES = 2
UPLOAD_RETRY_DELAY = 1.0
HTTP_CONNECT_TIMEOUT = 60
HTTP_READ_TIMEOUT = 1800  # 30 minutes

# WiFi channels (defaults from app)
DEFAULT_WIFI_CHANNELS = [149, 153]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("acemate-ota")


# ---------------------------------------------------------------------------
# Cloud API
# ---------------------------------------------------------------------------

def cloud_headers():
    """
    Build HTTP headers matching the app's interceptor.

    The app sends:
      User-Agent: {appVersion}|{deviceId}|{robotId}|{rkSoftwareVersion}|{timeZone}|Android
      X-Acemate-Staging: false
      Accept-Language: en
      Content-Type: application/json
    """
    return {
        "User-Agent": "1.4.3|acemate-ota-tool|||UTC|Python",
        "X-Acemate-Staging": "false",
        "Accept-Language": "en",
        "Content-Type": "application/json",
    }


def query_cloud_firmware_info():
    """
    Calls the AceMate cloud API to get firmware info.

    The app uses two endpoints:
      GET  /v1/app/init    → returns { code, data: { trainDataNum, wifiChannels, countryCode, activityUrl } }
      POST /v1/app/configs → returns { code, data: { otaPackageUrl, otaPackageName, isCloseForceOta, ... } }

    The OTA URL and filename come from /v1/app/configs.
    """
    log.info("Querying cloud API for firmware info...")
    headers = cloud_headers()

    # Step 1: app init (to get wifi channels etc.)
    try:
        resp = requests.get(f"{API_BASE_URL}{API_INIT}", headers=headers, timeout=30)
        resp.raise_for_status()
        init_data = resp.json()
        log.info(f"App init response: {json.dumps(init_data, indent=2)}")
    except Exception as e:
        log.warning(f"App init failed (non-fatal): {e}")
        init_data = None

    # Step 2: configs (contains OTA info)
    try:
        resp = requests.post(f"{API_BASE_URL}{API_CONFIGS}", headers=headers, timeout=30)
        resp.raise_for_status()
        configs_data = resp.json()
        log.info(f"Configs response: {json.dumps(configs_data, indent=2)}")
    except Exception as e:
        log.error(f"Configs request failed: {e}")
        configs_data = None

    if configs_data and configs_data.get("code") in (0, 200):
        data = configs_data.get("data", {})
        ota_url = data.get("otaPackageUrl", "")
        ota_name = data.get("otaPackageName", "")
        is_close_force = data.get("isCloseForceOta", True)
        log.info(f"OTA Package URL:  {ota_url}")
        log.info(f"OTA Package Name: {ota_name}")
        log.info(f"Force OTA closed: {is_close_force}")

        version = extract_version(ota_name)
        if version:
            log.info(f"Firmware version: {version}")

        return {"url": ota_url, "name": ota_name, "force_closed": is_close_force}
    else:
        log.error("Could not get firmware info from cloud API")
        if configs_data:
            log.error(f"Response: {configs_data}")
        return None


def download_firmware(url: str, filename: str, output_dir: str = ".") -> str:
    """Download firmware file from cloud URL."""
    output_path = os.path.join(output_dir, filename)

    if os.path.exists(output_path):
        log.info(f"Firmware already exists locally: {output_path}")
        return output_path

    log.info(f"Downloading firmware: {url} → {output_path}")

    resp = requests.get(url, stream=True, timeout=HTTP_CONNECT_TIMEOUT)
    resp.raise_for_status()

    total = int(resp.headers.get("content-length", 0))
    downloaded = 0

    with open(output_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = (downloaded / total) * 100
                print(f"\rDownloading: {pct:.1f}% ({downloaded}/{total})", end="", flush=True)

    print()
    log.info(f"Download complete: {output_path} ({downloaded} bytes)")
    return output_path


# ---------------------------------------------------------------------------
# BLE Communication
# ---------------------------------------------------------------------------

def make_ble_command(json_str: str) -> bytes:
    """Format a BLE command as the app does: JSON + \r\n + delimiter."""
    return (json_str + "\r\n" + BLE_MSG_DELIMITER).encode("utf-8")


def make_wifi_open_command(purpose: str = "ota", wifi_band: str = "5G",
                           channels: list = None) -> str:
    """Build the WiFi AP open request JSON (as BluetoothData.wifiOpenRequestJson does)."""
    if channels is None:
        channels = DEFAULT_WIFI_CHANNELS

    cmd = {
        "type": "ap",
        "data": {
            "purpose": purpose,
            "wifiBand": wifi_band,
            "wifi_channels": channels,
        }
    }
    return json.dumps(cmd, separators=(",", ":"))


def make_wifi_stop_command(purpose: str = "ota") -> str:
    """Build the WiFi AP stop request JSON."""
    cmd = {
        "type": "stop_ap",
        "data": {
            "purpose": purpose,
        }
    }
    return json.dumps(cmd, separators=(",", ":"))


async def ble_update_flow(firmware_path: str, wifi_band: str = "5G"):
    """
    Full BLE-driven update flow:
    1. Scan and connect to AceMate device via BLE
    2. Send WiFi AP open command
    3. Wait for WiFi SSID response
    4. Instruct user to connect to WiFi (or auto-connect on Linux)
    5. Upload firmware
    6. Trigger update
    7. Monitor MCU progress via BLE
    """
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        log.error("bleak package required for BLE. Install with: pip install bleak")
        sys.exit(1)

    # --- Scan for AceMate device ---
    log.info("Scanning for AceMate BLE devices...")
    discovered = await BleakScanner.discover(timeout=10.0, return_adv=True)

    # discovered is dict: {BLEDevice: AdvertisementData}
    acemate_devices = [
        (dev, adv) for dev, adv in discovered.values()
        if dev.name and "acemate" in dev.name.lower()
    ]
    if not acemate_devices:
        log.warning("No device with 'AceMate' in name found. Showing all devices:")
        for dev, adv in discovered.values():
            log.info(f"  {dev.address} - {dev.name or 'Unknown'} (RSSI: {adv.rssi})")
        addr = input("\nEnter device BLE address (XX:XX:XX:XX:XX:XX): ").strip()
    else:
        for i, (dev, adv) in enumerate(acemate_devices):
            log.info(f"  [{i}] {dev.address} - {dev.name} (RSSI: {adv.rssi})")
        if len(acemate_devices) == 1:
            addr = acemate_devices[0][0].address
            log.info(f"Auto-selecting: {addr}")
        else:
            idx = int(input("Select device index: "))
            addr = acemate_devices[idx][0].address

    # --- Connect ---
    log.info(f"Connecting to {addr}...")
    message_buffer = ""
    # Use a mutable container so the on_notify callback can always
    # resolve the *current* future (important for the 2.4G retry).
    wifi_ssid_holder = {"future": asyncio.get_event_loop().create_future()}
    mcu_progress = [0.0]

    def on_notify(sender, data: bytearray):
        nonlocal message_buffer
        message_buffer += data.decode("utf-8", errors="replace")

        # Check for complete message (delimited by +==ACE==+)
        while BLE_MSG_DELIMITER in message_buffer:
            msg, message_buffer = message_buffer.split(BLE_MSG_DELIMITER, 1)
            msg = msg.strip()
            if not msg:
                continue

            log.debug(f"BLE RX: {msg}")
            try:
                parsed = json.loads(msg)
                msg_type = parsed.get("type", "")

                if msg_type == "rsp_ap":
                    # WiFi AP response with SSID
                    data_obj = parsed.get("data", {})
                    ssid = data_obj.get("ssid", "")
                    purpose = data_obj.get("purpose", "")
                    log.info(f"Device WiFi AP ready: SSID='{ssid}', purpose='{purpose}'")
                    fut = wifi_ssid_holder["future"]
                    if not fut.done():
                        fut.set_result(ssid)

                elif msg_type == "mcu_ota_result":
                    progress = parsed.get("data", {}).get("Progress", 0)
                    mcu_progress[0] = progress
                    log.info(f"MCU Update Progress: {progress:.1f}%")

                elif msg_type == "rsp_version":
                    log.info(f"Device version: {json.dumps(parsed.get('data', {}), indent=2)}")

                else:
                    log.debug(f"BLE message: {msg_type}")

            except json.JSONDecodeError:
                log.debug(f"Non-JSON BLE data: {msg[:100]}")

    async def send_ble_cmd(client, cmd_str):
        """Send a BLE command, chunking to fit MTU."""
        cmd_bytes = make_ble_command(cmd_str)
        mtu = client.mtu_size - 3 if hasattr(client, "mtu_size") else 20
        for i in range(0, len(cmd_bytes), mtu):
            chunk = cmd_bytes[i:i + mtu]
            await client.write_gatt_char(NUS_TX_CHAR_UUID, chunk, response=False)
            await asyncio.sleep(BLE_SEND_INTERVAL)

    async with BleakClient(addr) as client:
        log.info(f"Connected: {client.is_connected}")

        # Subscribe to notifications
        await client.start_notify(NUS_RX_CHAR_UUID, on_notify)
        log.info("Subscribed to BLE notifications")

        # --- Send WiFi AP open command ---
        cmd = make_wifi_open_command("ota", wifi_band)
        log.info(f"Sending WiFi AP open command (band={wifi_band}): {cmd}")
        await send_ble_cmd(client, cmd)

        # --- Wait for SSID ---
        log.info("Waiting for device WiFi hotspot SSID (timeout: 60s)...")
        try:
            ssid = await asyncio.wait_for(wifi_ssid_holder["future"], timeout=60.0)
        except asyncio.TimeoutError:
            if wifi_band == "5G":
                log.warning("Timeout on 5G, retrying with 2.4G...")
                # Replace the future so the callback resolves the new one
                wifi_ssid_holder["future"] = asyncio.get_event_loop().create_future()

                cmd = make_wifi_open_command("ota", "2.4G")
                await send_ble_cmd(client, cmd)

                try:
                    ssid = await asyncio.wait_for(
                        wifi_ssid_holder["future"], timeout=60.0
                    )
                except asyncio.TimeoutError:
                    log.error("Timeout waiting for WiFi hotspot on both bands")
                    return False
            else:
                log.error("Timeout waiting for WiFi hotspot")
                return False

        # --- Prompt user to connect to WiFi ---
        print(f"\n{'='*60}")
        print(f"  Device WiFi hotspot is ready!")
        print(f"  SSID: {ssid}")
        print(f"  (Open network, no password)")
        print(f"")
        print(f"  Please connect to this WiFi network now.")
        print(f"  On Linux: nmcli device wifi connect '{ssid}'")
        print(f"{'='*60}\n")

        input("Press ENTER after you've connected to the WiFi hotspot...")

        # --- Upload and trigger ---
        success = upload_and_trigger(firmware_path)

        if success:
            # Monitor MCU progress via BLE
            log.info("Monitoring MCU update progress via BLE...")
            for _ in range(600):  # up to 10 minutes
                await asyncio.sleep(1)
                if mcu_progress[0] >= 100.0:
                    log.info("MCU update complete!")
                    break
            else:
                log.warning("MCU progress monitoring timed out")

        # Stop notifications
        await client.stop_notify(NUS_RX_CHAR_UUID)

    return success


# ---------------------------------------------------------------------------
# HTTP Upload & Trigger (works when already on robot's WiFi)
# ---------------------------------------------------------------------------

def upload_firmware(firmware_path: str, retries: int = UPLOAD_MAX_RETRIES) -> bool:
    """
    Upload firmware to robot via HTTP POST multipart.

    The app uploads with:
      - URL: http://10.42.0.1:5000/upload
      - Multipart field: "file"
      - Filename: "{original_name}.gz"
      - Content-Type: application/octet-stream
    """
    if not os.path.exists(firmware_path):
        log.error(f"Firmware file not found: {firmware_path}")
        return False

    file_size = os.path.getsize(firmware_path)
    filename = os.path.basename(firmware_path)

    # The app appends .gz to the filename in the multipart form
    # The file itself (e.g. .tar) may already be gzipped
    upload_filename = filename + ".gz" if not filename.endswith(".gz") else filename

    url = f"{RK_BASE_URL}{RK_OTA_UPLOAD}"
    log.info(f"Uploading firmware to {url}")
    log.info(f"  File: {firmware_path} ({file_size} bytes)")
    log.info(f"  Upload filename: {upload_filename}")

    for attempt in range(retries + 1):
        try:
            if attempt > 0:
                log.info(f"Retry {attempt}/{retries} after {UPLOAD_RETRY_DELAY}s...")
                time.sleep(UPLOAD_RETRY_DELAY)

            # Use MultipartEncoder for progress tracking
            encoder = MultipartEncoder(
                fields={
                    "file": (upload_filename, open(firmware_path, "rb"),
                             "application/octet-stream"),
                }
            )

            # Progress callback
            last_pct = [0]

            def progress_callback(monitor):
                pct = int((monitor.bytes_read / monitor.len) * 100)
                if pct != last_pct[0]:
                    last_pct[0] = pct
                    print(f"\rUploading: {pct}%", end="", flush=True)

            monitor = MultipartEncoderMonitor(encoder, progress_callback)

            resp = requests.post(
                url,
                data=monitor,
                headers={"Content-Type": monitor.content_type},
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
            )

            print()  # newline after progress

            log.info(f"Upload response: HTTP {resp.status_code}")
            log.info(f"Response body: {resp.text}")

            # Check success (app checks: code==0, code==200, or HTTP success)
            try:
                result = resp.json()
                code = result.get("code")
                if isinstance(code, float):
                    code = int(code)
                if code in (0, 200) or resp.ok:
                    log.info("Upload successful!")
                    return True
                else:
                    log.error(f"Upload failed: code={code}")
            except Exception:
                if resp.ok:
                    log.info("Upload successful (non-JSON response)")
                    return True
                else:
                    log.error(f"Upload failed: HTTP {resp.status_code}")

        except requests.exceptions.ConnectionError as e:
            log.error(f"Connection error: {e}")
            log.error("Are you connected to the robot's WiFi hotspot?")
        except Exception as e:
            log.error(f"Upload error: {e}")

    log.error("All upload retries exhausted")
    return False


def trigger_update() -> bool:
    """
    Trigger the device firmware update.
    POST http://10.42.0.1:5000/trigger_update
    """
    url = f"{RK_BASE_URL}{RK_TRIGGER_UPDATE}"
    log.info(f"Triggering device update: POST {url}")

    try:
        resp = requests.post(url, timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT))
        log.info(f"Trigger response: HTTP {resp.status_code}")
        log.info(f"Response body: {resp.text}")

        try:
            result = resp.json()
            code = result.get("code")
            if isinstance(code, float):
                code = int(code)
            if code in (0, 200) or resp.ok:
                log.info("Update triggered successfully!")
                return True
            else:
                log.error(f"Trigger failed: code={code}")
                return False
        except Exception:
            if resp.ok:
                log.info("Update triggered successfully (non-JSON response)")
                return True
            log.error(f"Trigger failed: HTTP {resp.status_code}")
            return False

    except requests.exceptions.ConnectionError as e:
        log.error(f"Connection error: {e}")
        log.error("Are you connected to the robot's WiFi hotspot?")
        return False
    except Exception as e:
        log.error(f"Trigger error: {e}")
        return False


def upload_and_trigger(firmware_path: str) -> bool:
    """Upload firmware and trigger update (when already on robot WiFi)."""
    if not upload_firmware(firmware_path):
        return False

    # Small delay before triggering (app does this via coroutine scheduling)
    time.sleep(1)

    return trigger_update()


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def extract_version(filename: str) -> str:
    """Extract version number from firmware filename (regex: \\d+\\.\\d+\\.\\d+)."""
    match = re.search(r"\d+\.\d+\.\d+", filename)
    return match.group(0) if match else None


def check_connectivity():
    """Check if we can reach the robot's HTTP server."""
    try:
        resp = requests.get(f"{RK_BASE_URL}/", timeout=5)
        log.info(f"Robot reachable: HTTP {resp.status_code}")
        return True
    except requests.exceptions.ConnectionError:
        log.warning("Cannot reach robot at 10.42.0.1:5000")
        log.warning("Make sure you are connected to the robot's WiFi hotspot")
        return False
    except Exception as e:
        log.warning(f"Connectivity check failed: {e}")
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AceMate Tennis Robot - Firmware Update Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Query cloud for available firmware
  %(prog)s --query

  # Download firmware from cloud
  %(prog)s --download

  # Upload firmware (already connected to robot WiFi)
  %(prog)s --upload-only firmware.tar

  # Full BLE flow (scan, connect, open hotspot, upload, trigger)
  %(prog)s --update firmware.tar

  # Use bundled firmware from APK
  %(prog)s --upload-only ota_package_acemate_1.4.3.tar
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--query", action="store_true",
                       help="Query cloud API for available firmware info")
    group.add_argument("--download", action="store_true",
                       help="Download firmware from cloud")
    group.add_argument("--upload-only", metavar="FILE",
                       help="Upload firmware and trigger update (must be on robot WiFi)")
    group.add_argument("--update", metavar="FILE",
                       help="Full BLE update flow (scan, connect, upload, trigger)")

    parser.add_argument("--wifi-band", choices=["5G", "2.4G"], default="5G",
                        help="WiFi band for hotspot (default: 5G)")
    parser.add_argument("--output-dir", default=".",
                        help="Directory to save downloaded firmware")
    parser.add_argument("--check", action="store_true",
                        help="Check connectivity to robot before uploading")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.query:
        info = query_cloud_firmware_info()
        if info:
            print(f"\nFirmware URL:  {info['url']}")
            print(f"Firmware Name: {info['name']}")
            version = extract_version(info['name'])
            if version:
                print(f"Version:       {version}")

    elif args.download:
        info = query_cloud_firmware_info()
        if not info or not info["url"]:
            log.error("No firmware URL available from cloud")
            sys.exit(1)
        path = download_firmware(info["url"], info["name"], args.output_dir)
        print(f"\nFirmware saved to: {path}")

    elif args.upload_only:
        firmware_path = args.upload_only
        if not os.path.exists(firmware_path):
            log.error(f"File not found: {firmware_path}")
            sys.exit(1)

        if args.check:
            if not check_connectivity():
                sys.exit(1)

        success = upload_and_trigger(firmware_path)
        sys.exit(0 if success else 1)

    elif args.update:
        firmware_path = args.update
        if not os.path.exists(firmware_path):
            log.error(f"File not found: {firmware_path}")
            sys.exit(1)

        try:
            success = asyncio.run(ble_update_flow(firmware_path, args.wifi_band))
            sys.exit(0 if success else 1)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            sys.exit(1)


if __name__ == "__main__":
    main()
