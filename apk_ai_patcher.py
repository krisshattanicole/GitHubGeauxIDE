#!/usr/bin/env python3
"""
APK AI Patcher - One-click unpack, AI patch, repack & sign.
Requires: Java, Apktool, Android SDK (apksigner), Ollama.
"""

import os
import sys
import json
import shutil
import subprocess
import tempfile
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox
from pathlib import Path
import threading
import requests

# ----------------------------------------------------------------------
# CONFIGURATION - ADJUST THESE PATHS TO MATCH YOUR SYSTEM
# ----------------------------------------------------------------------
APKTOOL_PATH = r"C:\tools\apktool.jar"          # Path to apktool.jar
ANDROID_SDK_PATH = r"C:\Android\Sdk"            # Android SDK root
BUILD_TOOLS_VERSION = "34.0.0"                  # Adjust to your installed version
KEYSTORE_PATH = os.path.expanduser("~/.android/debug.keystore")
KEYSTORE_PASS = "android"
KEY_ALIAS = "androiddebugkey"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "codellama:13b"                   # or "codellama:7b", "llama3", etc.

# ----------------------------------------------------------------------
# Helper functions to locate SDK tools
# ----------------------------------------------------------------------

def find_apksigner():
    candidates = [
        os.path.join(ANDROID_SDK_PATH, "build-tools", BUILD_TOOLS_VERSION, "apksigner"),
        os.path.join(ANDROID_SDK_PATH, "build-tools", BUILD_TOOLS_VERSION, "apksigner.bat"),
        "apksigner"  # hope it's in PATH
    ]
    for c in candidates:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return None


def find_zipalign():
    candidates = [
        os.path.join(ANDROID_SDK_PATH, "build-tools", BUILD_TOOLS_VERSION, "zipalign"),
        os.path.join(ANDROID_SDK_PATH, "build-tools", BUILD_TOOLS_VERSION, "zipalign.exe"),
        "zipalign"
    ]
    for c in candidates:
        if shutil.which(c) or os.path.isfile(c):
            return c
    return None


APKSIGNER = find_apksigner()
ZIPALIGN = find_zipalign()

# ----------------------------------------------------------------------
# Create a debug keystore if it doesn't exist
# ----------------------------------------------------------------------

def ensure_keystore():
    if not os.path.exists(KEYSTORE_PATH):
        ks_dir = os.path.dirname(KEYSTORE_PATH)
        if not os.path.exists(ks_dir):
            os.makedirs(ks_dir)
        cmd = [
            "keytool", "-genkey", "-v", "-keystore", KEYSTORE_PATH,
            "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "10000", "-storepass", KEYSTORE_PASS,
            "-keypass", KEYSTORE_PASS, "-dname", "CN=Debug, OU=Debug, O=Debug, L=Debug, ST=Debug, C=US"
        ]
        subprocess.run(cmd, check=True)
        print(f"[✓] Created debug keystore at {KEYSTORE_PATH}")

# ----------------------------------------------------------------------
# APK manipulation using apktool
# ----------------------------------------------------------------------

def decode_apk(apk_path, out_dir):
    cmd = ["java", "-jar", APKTOOL_PATH, "d", "-f", "-o", out_dir, apk_path]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def rebuild_apk(work_dir, output_apk):
    cmd = ["java", "-jar", APKTOOL_PATH, "b", "-o", output_apk, work_dir]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def sign_apk(apk_path):
    # First zipalign (if available)
    if ZIPALIGN:
        aligned = apk_path + ".aligned"
        subprocess.run([ZIPALIGN, "-p", "-f", "4", apk_path, aligned], check=True)
        os.replace(aligned, apk_path)
    # Then sign
    if APKSIGNER:
        cmd = [APKSIGNER, "sign", "--ks", KEYSTORE_PATH,
               "--ks-pass", f"pass:{KEYSTORE_PASS}",
               "--ks-key-alias", KEY_ALIAS,
               "--key-pass", f"pass:{KEYSTORE_PASS}",
               apk_path]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    else:
        # fallback to jarsigner
        subprocess.run(["jarsigner", "-verbose", "-sigalg", "SHA1withRSA",
                        "-digestalg", "SHA1", "-keystore", KEYSTORE_PATH,
                        "-storepass", KEYSTORE_PASS, "-keypass", KEYSTORE_PASS,
                        apk_path, KEY_ALIAS], check=True)

# ----------------------------------------------------------------------
# AI interaction with Ollama
# ----------------------------------------------------------------------

def ask_ai_for_patches(work_dir, user_request):
    """
    Sends all smali file contents + user request to Ollama.
    Expects a JSON response with list of modifications.
    """
    smali_files = list(Path(work_dir).rglob("*.smali"))
    # Build context: for each smali file, include its path and first 50 lines (to keep prompt size manageable)
    context = ""
    for smali in smali_files[:20]:  # limit to 20 files to avoid huge prompts
        rel_path = smali.relative_to(work_dir)
        with open(smali, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read(5000)  # first 5000 chars per file
        context += f"\n--- FILE: {rel_path} ---\n{content}\n"

    prompt = f"""You are an expert in Android smali code. Given the following smali files from an APK, 
and a user's patch request, output a JSON array of modifications needed.

User request: {user_request}

Files content:
{context}

Respond ONLY with a JSON array in this exact format:
[
  {{
    "file": "path/relative/to/workdir/file.smali",
    "line": 42,
    "new_content": "the exact smali code to replace that line"
  }}
]
If multiple lines need to be replaced, use separate objects. If a whole method needs replacement, include all lines.
Make sure the new_content is valid smali.
"""
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json"
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        response_text = data.get("response", "")
        # Extract JSON from response (might be surrounded by markdown)
        json_start = response_text.find('[')
        json_end = response_text.rfind(']') + 1
        if json_start != -1 and json_end > json_start:
            json_str = response_text[json_start:json_end]
            patches = json.loads(json_str)
            return patches
        else:
            return {"error": "No JSON found in AI response"}
    except Exception as e:
        return {"error": str(e)}


def apply_patches(work_dir, patches):
    work_dir_path = Path(work_dir).resolve()
    for p in patches:
        file_path = (Path(work_dir) / p["file"]).resolve()
        if not str(file_path).startswith(f"{work_dir_path}{os.sep}"):
            continue
        if not file_path.exists():
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        line_no = p["line"] - 1  # to 0-index
        if 0 <= line_no < len(lines):
            lines[line_no] = p["new_content"] + "\n"
        with open(file_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

# ----------------------------------------------------------------------
# GUI Application
# ----------------------------------------------------------------------

class APKPatcherApp:
    def __init__(self, root):
        self.root = root
        root.title("APK AI Patcher")
        root.geometry("700x600")

        # APK selection
        tk.Label(root, text="Select APK file:").pack(pady=5)
        self.apk_path_var = tk.StringVar()
        tk.Entry(root, textvariable=self.apk_path_var, width=70).pack(pady=2)
        tk.Button(root, text="Browse", command=self.browse_apk).pack(pady=2)

        # Patch description
        tk.Label(root, text="Describe the patch you want (e.g., 'remove ads', 'make in-app purchases free'):").pack(pady=10)
        self.patch_text = tk.Text(root, height=5, width=80)
        self.patch_text.pack(pady=5)

        # Run button
        self.run_btn = tk.Button(root, text="Run AI Patch (One Click)", command=self.run_patch, bg="green", fg="white", font=("Arial", 12))
        self.run_btn.pack(pady=10)

        # Log output
        self.log = scrolledtext.ScrolledText(root, height=20, state='normal')
        self.log.pack(pady=10, fill=tk.BOTH, expand=True)

        # Status bar
        self.status = tk.Label(root, text="Ready", bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status.pack(side=tk.BOTTOM, fill=tk.X)

    def browse_apk(self):
        filename = filedialog.askopenfilename(filetypes=[("APK files", "*.apk")])
        if filename:
            self.apk_path_var.set(filename)

    def log_message(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.root.update()

    def run_patch(self):
        apk = self.apk_path_var.get()
        desc = self.patch_text.get("1.0", tk.END).strip()
        if not apk or not os.path.isfile(apk):
            messagebox.showerror("Error", "Please select a valid APK file.")
            return
        if not desc:
            messagebox.showerror("Error", "Please enter a patch description.")
            return

        # Run in a separate thread to keep GUI responsive
        threading.Thread(target=self._run_patch_thread, args=(apk, desc), daemon=True).start()

    def _run_patch_thread(self, apk_path, description):
        try:
            self.run_btn.config(state=tk.DISABLED)
            self.status.config(text="Working...")
            self.log_message("=== Starting APK patching process ===")

            # Create temporary working directory
            with tempfile.TemporaryDirectory() as tmpdir:
                work_dir = os.path.join(tmpdir, "decoded")
                self.log_message("[1] Decoding APK with apktool...")
                decode_apk(apk_path, work_dir)
                self.log_message("    Decoding done.")

                # Ask AI for patches
                self.log_message("[2] Asking AI (Ollama) for patches...")
                patches = ask_ai_for_patches(work_dir, description)
                if isinstance(patches, dict) and "error" in patches:
                    self.log_message(f"    AI error: {patches['error']}")
                    return
                self.log_message(f"    AI returned {len(patches)} modifications.")

                # Apply patches
                self.log_message("[3] Applying patches...")
                apply_patches(work_dir, patches)
                self.log_message("    Patches applied.")

                # Rebuild
                unsigned_apk = os.path.join(tmpdir, "unsigned.apk")
                self.log_message("[4] Rebuilding APK...")
                rebuild_apk(work_dir, unsigned_apk)
                self.log_message("    Rebuild done.")

                # Sign
                self.log_message("[5] Signing APK...")
                ensure_keystore()
                sign_apk(unsigned_apk)
                self.log_message("    Signing done.")

                # Move final APK to original folder with "_patched" suffix
                final_name = apk_path.replace(".apk", "_patched.apk")
                shutil.move(unsigned_apk, final_name)
                self.log_message(f"[✓] Success! Patched APK saved as: {final_name}")

                self.status.config(text="Completed")
                messagebox.showinfo("Success", f"Patched APK saved as:\n{final_name}")
        except Exception as e:
            self.log_message(f"ERROR: {str(e)}")
            self.status.config(text="Error")
            messagebox.showerror("Error", str(e))
        finally:
            self.run_btn.config(state=tk.NORMAL)

# ----------------------------------------------------------------------
if __name__ == "__main__":
    # Quick checks for required tools
    missing = []
    if not os.path.isfile(APKTOOL_PATH):
        missing.append("Apktool.jar (update APKTOOL_PATH)")
    if not APKSIGNER:
        missing.append("apksigner (update ANDROID_SDK_PATH or BUILD_TOOLS_VERSION)")
    if missing:
        print("Missing dependencies:\n" + "\n".join(missing))
        print("Please adjust the paths at the top of the script.")
        sys.exit(1)

    root = tk.Tk()
    app = APKPatcherApp(root)
    root.mainloop()
