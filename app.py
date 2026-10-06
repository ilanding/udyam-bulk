#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Udyam Bulk Certificate Downloader — Web App
==========================================
Browser me khulta hai: URN list paste karo, har CAPTCHA khud solve karo,
script Verify + Download karta hai, end me ZIP milta hai.

Server pe real Chromium (Playwright) chalta hai kyunki portal simple
requests wale automation ko block kar deta hai.
"""

import os
import re
import time
import uuid
import threading
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_file, render_template

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)

app = Flask(__name__)

HOME_URL = "https://www.udyamregistration.gov.in/"
VERIFY_URL = "https://www.udyamregistration.gov.in/Udyam_Verify.aspx"

SEL_URN = 'input[name="ctl00$ContentPlaceHolder1$txtUdyamNo"]'
SEL_CAPTCHA = 'input[name="ctl00$ContentPlaceHolder1$txtCaptcha"]'
SEL_VERIFY = 'input[name="ctl00$ContentPlaceHolder1$btnVerify"]'
SEL_DL = ('input[value="Download Certificate"], '
          'button:has-text("Download Certificate"), '
          'a:has-text("Download Certificate")')
SEL_CAPTCHA_IMG = 'img[src*="Captcha"]'

NAV_TIMEOUT = 60000
CAPTCHA_WAIT = 600  # ek captcha ke liye max 10 min wait
MAX_URNS = 100

jobs = {}
jobs_lock = threading.Lock()


def get_proxy():
    """Agar HTTPS_PROXY/HTTP_PROXY env set hai to Playwright ko do (warna None)."""
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if not v:
            continue
        u = urlparse(v)
        if not u.hostname:
            continue
        d = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 8080}"}
        if u.username:
            d["username"] = u.username
        if u.password:
            d["password"] = u.password
        return d
    return None


def safe_name(urn):
    return re.sub(r"[^A-Za-z0-9_-]", "_", urn)


def update_zip(job):
    zp = job["dir"] / "certificates.zip"
    try:
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for pdf in sorted(job["dir"].glob("*.pdf")):
                z.write(pdf, pdf.name)
    except Exception:
        pass


def goto_verify(page):
    for _ in range(2):
        try:
            page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception:
            time.sleep(3)
            continue
        try:
            page.wait_for_selector(SEL_URN, timeout=20000)
            return True
        except Exception:
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                time.sleep(2)
                page.goto(VERIFY_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                page.wait_for_selector(SEL_URN, timeout=20000)
                return True
            except Exception:
                time.sleep(3)
    return False


def process_urn(page, job, urn):
    pdf_path = job["dir"] / (safe_name(urn) + ".pdf")
    if pdf_path.exists():
        job["done"].append(urn)
        return

    if not goto_verify(page):
        job["failed"].append({"urn": urn, "reason": "verify page nahi khula"})
        return

    try:
        page.fill(SEL_URN, urn)
    except Exception:
        job["failed"].append({"urn": urn, "reason": "form nahi bhara"})
        return

    for _ in range(3):
        # captcha ka screenshot lo aur user ke solve ka wait karo
        try:
            img = page.locator(SEL_CAPTCHA_IMG).first
            img.screenshot(path=str(job["captcha_file"]))
        except Exception:
            try:
                page.screenshot(path=str(job["captcha_file"]))
            except Exception:
                job["failed"].append({"urn": urn, "reason": "captcha image nahi mila"})
                return

        job["state"] = "awaiting_captcha"
        job["captcha_ts"] = time.time()
        job["event"].clear()
        job["solution"] = None
        got = job["event"].wait(timeout=CAPTCHA_WAIT)
        if job.get("stop"):
            return  # naya job shuru ho gaya — chupchaap bahar, fail me mat gino
        sol = job.get("solution")
        job["state"] = "working"

        if not got or not sol:
            job["failed"].append({"urn": urn, "reason": "captcha ka jawab nahi aaya (timeout)"})
            return

        try:
            page.fill(SEL_CAPTCHA, sol)
            page.click(SEL_VERIFY)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
        except Exception:
            time.sleep(3)

        try:
            html = page.content()
        except Exception:
            html = ""

        if "Incorrect verification code" in html:
            if not goto_verify(page):
                job["failed"].append({"urn": urn, "reason": "retry pe page nahi khula"})
                return
            try:
                page.fill(SEL_URN, urn)
            except Exception:
                pass
            continue

        if "PrintUdyamApplication" in page.url or "UDYAM REGISTRATION CERTIFICATE" in html:
            try:
                with page.expect_download(timeout=30000) as dl_info:
                    page.locator(SEL_DL).first.click()
                dl = dl_info.value
                dl.save_as(pdf_path)
                job["done"].append(urn)
                update_zip(job)
            except Exception:
                job["failed"].append({"urn": urn, "reason": "Download Certificate se file nahi aayi"})
            return

        job["failed"].append({"urn": urn, "reason": "anjaana result (URN galat ho sakta hai)"})
        return

    job["failed"].append({"urn": urn, "reason": "captcha 3 baar fail"})


def run_job(job):
    from playwright.sync_api import sync_playwright
    job["state"] = "working"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            ], proxy=get_proxy())
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            except Exception:
                pass
            for idx, urn in enumerate(job["urns"], 1):
                if job.get("stop"):
                    break
                job["idx"] = idx
                job["current_urn"] = urn
                try:
                    process_urn(page, job, urn)
                except Exception as e:
                    job["failed"].append({"urn": urn, "reason": f"error: {e}"})
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        job["failed"].append({"urn": "-", "reason": f"browser error: {e}"})
    finally:
        job["state"] = "done"
        job["current_urn"] = None
        update_zip(job)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    raw = data.get("urns", "")
    urns, seen = [], set()
    for line in str(raw).splitlines():
        line = line.strip().strip(",").strip('"').strip("'")
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            urns.append(line)
    urns = urns[:MAX_URNS]
    if not urns:
        return jsonify({"ok": False, "error": "koi URN nahi mila"}), 400

    with jobs_lock:
        # purana job roko — turant jagao taaki uska browser band ho jaye
        for j in jobs.values():
            j["stop"] = True
            try:
                j["event"].set()
            except Exception:
                pass
        job_id = uuid.uuid4().hex[:12]
        job_dir = DATA / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job = {
            "id": job_id,
            "dir": job_dir,
            "urns": urns,
            "total": len(urns),
            "idx": 0,
            "current_urn": None,
            "done": [],
            "failed": [],
            "state": "starting",
            "captcha_file": job_dir / "captcha.png",
            "captcha_ts": 0,
            "event": threading.Event(),
            "solution": None,
            "stop": False,
        }
        jobs[job_id] = job
        t = threading.Thread(target=run_job, args=(job,), daemon=True)
        job["thread"] = t
        t.start()
    return jsonify({"ok": True, "job_id": job_id, "total": len(urns)})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False, "error": "job nahi mila"}), 404
    return jsonify({
        "ok": True,
        "state": job["state"],
        "total": job["total"],
        "idx": job["idx"],
        "current_urn": job["current_urn"],
        "done_count": len(job["done"]),
        "done": job["done"][-10:],
        "failed": job["failed"],
        "captcha_ts": job["captcha_ts"],
    })


@app.route("/api/solve", methods=["POST"])
def api_solve():
    data = request.get_json(force=True, silent=True) or {}
    job = jobs.get(data.get("job_id", ""))
    sol = str(data.get("solution", "")).strip()
    if not job:
        return jsonify({"ok": False, "error": "job nahi mila"}), 404
    if job["state"] != "awaiting_captcha":
        return jsonify({"ok": False, "error": "abhi captcha ka wait nahi hai"}), 400
    if not sol:
        return jsonify({"ok": False, "error": "khaali jawab nahi chalega"}), 400
    job["solution"] = sol
    job["event"].set()
    return jsonify({"ok": True})


@app.route("/api/captcha/<job_id>")
def api_captcha(job_id):
    job = jobs.get(job_id)
    if not job or not job["captcha_file"].exists():
        return jsonify({"ok": False}), 404
    resp = send_file(job["captcha_file"], mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/zip/<job_id>")
def api_zip(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"ok": False}), 404
    zp = job["dir"] / "certificates.zip"
    if not zp.exists():
        return jsonify({"ok": False, "error": "abhi koi certificate download nahi hua"}), 404
    return send_file(zp, as_attachment=True, download_name="udyam-certificates.zip")


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
