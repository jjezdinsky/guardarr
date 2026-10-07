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

SONARR_DOWNLOAD_DIR = os.environ.get("SONARR_DOWNLOAD_DIR", "/data/downloads/sonarr").rstrip("/")
RADARR_DOWNLOAD_DIR = os.environ.get("RADARR_DOWNLOAD_DIR", "/data/downloads/radarr").rstrip("/")
SWEEP_INTERVAL      = int(os.environ.get("SWEEP_INTERVAL", 60))

ARRS = {
    "sonarr": (SONARR_URL, SONARR_API_KEY, SONARR_DOWNLOAD_DIR),
    "radarr": (RADARR_URL, RADARR_API_KEY, RADARR_DOWNLOAD_DIR),
}

_tx_session_id   = ""
_tx_session_lock = threading.Lock()

# Hashes currently being handled — webhook and sweep must not race each other
_handled      = set()
_handled_lock = threading.Lock()

_last_sweep_ok = 0.0
_warned_foreign = set()


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


def file_ext(name: str) -> str:
    return os.path.splitext(name.split("/")[-1])[1].lstrip(".").lower()


def forbidden_files(files):
    return [f["name"] for f in files if file_ext(f["name"]) not in ALLOWED]


def get_torrent_files(download_id: str):
    """
    Poll Transmission until the torrent's file list is available.
    Returns the Transmission file list or None on timeout (the sweep picks it up later).
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
                return torrents[0]["files"]
        except Exception as exc:
            log.warning("torrent-get attempt %d failed: %s", attempt + 1, exc)
        time.sleep(2)
    return None


def remove_from_transmission(download_id: str):
    try:
        transmission_rpc("torrent-remove", {
            "ids": [download_id.lower()],
            "delete-local-data": True,
        })
        log.info("Removed torrent %s from Transmission (delete-local-data)", download_id)
    except Exception as exc:
        log.error("Transmission remove failed for %s: %s", download_id, exc)


def mark_failed_in_arr(arr: str, download_id: str):
    """
    Blocklist via history instead of queue: the grabbed history record exists right after
    the grab, whereas the queue item only appears after arr refreshes monitored downloads.
    """
    arr_url, api_key, _ = ARRS[arr]
    headers = {"X-Api-Key": api_key}

    for attempt in range(12):  # ~60 s — webhook can arrive before the history record is written
        resp = requests.get(
            f"{arr_url}/api/v3/history",
            params={"downloadId": download_id, "pageSize": 50,
                    "sortKey": "date", "sortDirection": "descending"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        records = [
            r for r in resp.json().get("records", [])
            if (r.get("downloadId") or "").upper() == download_id.upper()
        ]
        records.sort(key=lambda r: r["date"], reverse=True)

        if records:
            latest = records[0]
            if latest["eventType"] == "downloadFailed":
                log.info("[%s] %s already marked as failed in arr", arr, download_id)
                return
            if latest["eventType"] == "grabbed":
                fail = requests.post(
                    f"{arr_url}/api/v3/history/failed/{latest['id']}",
                    headers=headers,
                    timeout=10,
                )
                fail.raise_for_status()
                log.info("[%s] Marked history %d as failed — blocklisted (downloadId=%s)",
                         arr, latest["id"], download_id)
                return
            log.warning("[%s] Latest history event for %s is %s — not touching it",
                        arr, download_id, latest["eventType"])
            return
        time.sleep(5)

    log.warning("[%s] No grabbed history for %s — removed from Transmission, but not blocklisted",
                arr, download_id)


def block(arr: str, download_id: str, bad):
    log.warning("BLOCKED — %d forbidden file(s) in %s: %s", len(bad), download_id, bad)
    # Removing the data is what matters; it must not depend on arr bookkeeping succeeding
    remove_from_transmission(download_id)
    try:
        mark_failed_in_arr(arr, download_id)
    except Exception as exc:
        log.error("[%s] Blocklist call failed for %s: %s", arr, download_id, exc)


def claim(download_id: str) -> bool:
    with _handled_lock:
        if download_id.upper() in _handled:
            return False
        _handled.add(download_id.upper())
        return True


def release(download_id: str):
    with _handled_lock:
        _handled.discard(download_id.upper())


def check_and_block(arr: str, download_id: str, title: str):
    log.info("[%s] Checking %s [%s]", arr, title, download_id)
    if not claim(download_id):
        log.info("[%s] %s already being handled", arr, download_id)
        return

    try:
        files = get_torrent_files(download_id)
        if files is None:
            log.warning("Torrent %s has no file list after 90 s — leaving it to the sweep", download_id)
            return

        bad = forbidden_files(files)
        if not bad:
            log.info("OK — %d file(s), all allowed [%s]", len(files), download_id)
            return
        block(arr, download_id, bad)
    finally:
        release(download_id)


def arr_for_dir(download_dir: str):
    d = (download_dir or "").rstrip("/")
    for arr, (_, _, arr_dir) in ARRS.items():
        if d == arr_dir or d.startswith(arr_dir + "/"):
            return arr
    return None


def sweep_once():
    result = transmission_rpc("torrent-get", {
        "fields": ["hashString", "name", "files", "downloadDir"],
    })
    for t in result.get("arguments", {}).get("torrents", []):
        files = t.get("files") or []
        if not files:
            continue
        bad = forbidden_files(files)
        if not bad:
            continue

        download_id = t["hashString"].upper()
        arr = arr_for_dir(t.get("downloadDir"))
        if arr is None:
            if download_id not in _warned_foreign:
                _warned_foreign.add(download_id)
                log.warning("Sweep: forbidden files in non-arr torrent %s (%s, dir %s) — not touching it",
                            t.get("name"), download_id, t.get("downloadDir"))
            continue

        if not claim(download_id):
            continue
        log.warning("Sweep: caught %s [%s] missed by webhook", t.get("name"), download_id)
        try:
            block(arr, download_id, bad)
        finally:
            release(download_id)


def sweep_loop():
    global _last_sweep_ok
    while True:
        try:
            sweep_once()
            _last_sweep_ok = time.time()
        except Exception as exc:
            log.error("Sweep failed: %s", exc)
        time.sleep(SWEEP_INTERVAL)


def handle_grab(arr: str, data: dict):
    download_id = data.get("downloadId")
    if not download_id:
        log.warning("Grab event missing downloadId — skipping")
        return
    title = data.get("release", {}).get("releaseTitle", "unknown")
    threading.Thread(
        target=check_and_block,
        args=(arr, download_id, title),
        daemon=True,
    ).start()


def hook(arr: str):
    data = request.get_json(silent=True) or {}
    event = data.get("eventType")
    if event == "Test":
        log.info("%s test ping OK", arr.capitalize())
        return jsonify({"status": "ok"}), 200
    if event != "Grab":
        return jsonify({"status": "ignored"}), 200
    handle_grab(arr, data)
    return jsonify({"status": "ok"}), 200


@app.route("/hook/sonarr", methods=["POST"])
def sonarr_hook():
    return hook("sonarr")


@app.route("/hook/radarr", methods=["POST"])
def radarr_hook():
    return hook("radarr")


@app.route("/health")
def health():
    age = time.time() - _last_sweep_ok
    if age > SWEEP_INTERVAL * 5:
        return jsonify({"status": "sweep stale", "last_sweep_age_s": int(age)}), 503
    return jsonify({"status": "ok", "last_sweep_age_s": int(age)}), 200


# Started at import: gunicorn runs a single worker without --preload, so this lives in the worker
threading.Thread(target=sweep_loop, daemon=True).start()


if __name__ == "__main__":
    log.info("guardarr starting on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
