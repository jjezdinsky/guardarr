import os
import time
import logging
import threading
import requests
from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

ALLOWED = {
    "mkv", "mp4", "avi", "mov", "ts", "m2ts", "wmv",
    "srt", "ass", "ssa", "sub", "idx",
    "nfo", "jpg", "png", "txt",
}

TRANSMISSION_URL  = os.environ["TRANSMISSION_URL"]
TRANSMISSION_USER = os.environ["TRANSMISSION_USER"]
TRANSMISSION_PASS = os.environ["TRANSMISSION_PASS"]
SONARR_URL        = os.environ["SONARR_URL"]
SONARR_API_KEY    = os.environ["SONARR_API_KEY"]
RADARR_URL        = os.environ["RADARR_URL"]
RADARR_API_KEY    = os.environ["RADARR_API_KEY"]
PORT              = int(os.environ.get("WEBHOOK_PORT", 8978))

_tx_session_id   = ""
_tx_session_lock = threading.Lock()


def transmission_rpc(method, arguments=None):
    global _tx_session_id
    payload = {"method": method}
    if arguments:
        payload["arguments"] = arguments

    for _ in range(3):
        with _tx_session_lock:
            sid = _tx_session_id
        resp = requests.post(
            TRANSMISSION_URL,
            auth=(TRANSMISSION_USER, TRANSMISSION_PASS),
            headers={"X-Transmission-Session-Id": sid},
            json=payload,
            timeout=10,
        )
        if resp.status_code == 409:
            new_sid = resp.headers.get("X-Transmission-Session-Id", "")
            with _tx_session_lock:
                _tx_session_id = new_sid
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Cannot obtain Transmission session ID")


def get_torrent_files(download_id: str):
    """
    Poll Transmission until the torrent's file list is available.
    Returns list of (full_path, extension) or None on timeout.
    """
    time.sleep(5)
    for attempt in range(43):  # 5 + 43*2 = ~91 s total
        try:
            result = transmission_rpc("torrent-get", {
                "fields": ["files", "hashString"],
                "ids": [download_id.lower()],
            })
            torrents = result.get("arguments", {}).get("torrents", [])
            if torrents and torrents[0].get("files"):
                return [
                    (
                        f["name"],
                        os.path.splitext(f["name"].split("/")[-1])[1].lstrip(".").lower(),
                    )
                    for f in torrents[0]["files"]
                ]
        except Exception as exc:
            log.warning("torrent-get attempt %d failed: %s", attempt + 1, exc)
        time.sleep(2)
    return None


def blocklist_in_arr(arr_url: str, api_key: str, download_id: str):
    headers = {"X-Api-Key": api_key}

    resp = requests.get(
        f"{arr_url}/api/v3/queue",
        params={"downloadId": download_id, "pageSize": 100},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    records = resp.json().get("records", [])

    if not records:
        log.warning("Queue item %s not found in arr — cannot blocklist", download_id)
        return

    queue_id = records[0]["id"]
    del_resp = requests.delete(
        f"{arr_url}/api/v3/queue/{queue_id}",
        params={"blocklist": "true", "removeFromClient": "true"},
        headers=headers,
        timeout=10,
    )
    del_resp.raise_for_status()
    log.info("Blocklisted queue item %d (downloadId=%s)", queue_id, download_id)

    try:
        transmission_rpc("torrent-remove", {
            "ids": [download_id.lower()],
            "delete-local-data": True,
        })
        log.info("Removed torrent %s from Transmission (delete-local-data)", download_id)
    except Exception as exc:
        log.error("Transmission remove failed for %s: %s", download_id, exc)


def check_and_block(download_id: str, arr_url: str, api_key: str, title: str):
    log.info("Checking %s [%s]", title, download_id)

    files = get_torrent_files(download_id)
    if files is None:
        log.warning("Torrent %s not found in Transmission after 90 s — giving up", download_id)
        return

    bad = [(name, ext) for name, ext in files if ext not in ALLOWED]
    if not bad:
        log.info("OK — %d file(s), all allowed [%s]", len(files), download_id)
        return

    log.warning(
        "BLOCKED — %d forbidden file(s) in %s: %s",
        len(bad), download_id, [b[0] for b in bad],
    )
    try:
        blocklist_in_arr(arr_url, api_key, download_id)
    except Exception as exc:
        log.error("Blocklist call failed for %s: %s", download_id, exc)


def handle_grab(data: dict, arr_url: str, api_key: str):
    download_id = data.get("downloadId")
    if not download_id:
        log.warning("Grab event missing downloadId — skipping")
        return
    title = data.get("release", {}).get("releaseTitle", "unknown")
    threading.Thread(
        target=check_and_block,
        args=(download_id, arr_url, api_key, title),
        daemon=True,
    ).start()


@app.route("/hook/sonarr", methods=["POST"])
def sonarr_hook():
    data = request.get_json(silent=True) or {}
    event = data.get("eventType")
    if event == "Test":
        log.info("Sonarr test ping OK")
        return jsonify({"status": "ok"}), 200
    if event != "Grab":
        return jsonify({"status": "ignored"}), 200
    handle_grab(data, SONARR_URL, SONARR_API_KEY)
    return jsonify({"status": "ok"}), 200


@app.route("/hook/radarr", methods=["POST"])
def radarr_hook():
    data = request.get_json(silent=True) or {}
    event = data.get("eventType")
    if event == "Test":
        log.info("Radarr test ping OK")
        return jsonify({"status": "ok"}), 200
    if event != "Grab":
        return jsonify({"status": "ignored"}), 200
    handle_grab(data, RADARR_URL, RADARR_API_KEY)
    return jsonify({"status": "ok"}), 200


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    log.info("guardarr starting on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
