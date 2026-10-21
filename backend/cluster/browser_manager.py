import os
import time
import subprocess
import urllib.request
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

class ChromiumInstance:
    def __init__(self, instance_id: int, cdp_port: int, profile_dir: Path, proxy: Optional[str] = None):
        self.instance_id = instance_id
        self.cdp_port = cdp_port
        self.profile_dir = profile_dir
        self.proxy = proxy
        self.process: Optional[subprocess.Popen] = None

    def is_alive(self) -> bool:
        """Check via the CDP port whether the browser is alive."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/version", timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def get_tabs(self) -> List[Dict[str, Any]]:
        """Get all open pages (tabs) in this browser."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json", timeout=2.0) as resp:
                tabs = json.loads(resp.read().decode())
                return [t for t in tabs if t.get("type") == "page"]
        except Exception:
            return []

class BrowserClusterManager:
    """
    Launches several (e.g. 4) independent Chromium instances on the server,
    each with its own profile, proxy and CDP port.
    """

    def __init__(self, ports: List[int], profiles_root: Path, proxies_file: Optional[Path] = None):
        self.ports = ports
        self.profiles_root = profiles_root
        self.proxies_file = proxies_file
        self.instances: Dict[int, ChromiumInstance] = {}
        self.proxies: List[str] = self._load_proxies()

    def _load_proxies(self) -> List[str]:
        if not self.proxies_file or not self.proxies_file.exists():
            return []
        with open(self.proxies_file, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
        return lines

    def initialize_instances(self):
        """Configure all instances."""
        self.profiles_root.mkdir(parents=True, exist_ok=True)
        for idx, port in enumerate(self.ports):
            inst_id = idx + 1
            profile_dir = self.profiles_root / f"profile_{inst_id}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            
            # Assign proxies round-robin if any are available
            assigned_proxy = self.proxies[idx % len(self.proxies)] if self.proxies else None
            instance = ChromiumInstance(inst_id, port, profile_dir, assigned_proxy)
            self.instances[port] = instance

    def launch_instance(self, port: int, chromium_bin: str = "chromium"):
        """Launch the Chromium instance on the given port."""
        inst = self.instances.get(port)
        if not inst:
            return None

        if inst.is_alive():
            print(f"Browser #{inst.instance_id} is already running on port {port}.")
            return inst

        cmd = [
            chromium_bin,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={inst.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            "--start-maximized"
        ]

        # Attach the proxy
        if inst.proxy:
            cmd.append(f"--proxy-server={inst.proxy}")
            print(f"Browser #{inst.instance_id} starting with proxy: {inst.proxy}")

        # For a headless server environment (no X11)
        if os.getenv("HEADLESS_SERVER", "false").lower() == "true":
            cmd.extend(["--headless=new", "--disable-gpu"])

        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        inst.process = process
        print(f"Browser #{inst.instance_id} started (PID: {process.pid}, port: {port}).")
        return inst

    def launch_all(self, chromium_bin: str = "chromium"):
        """Launch all configured browsers."""
        self.initialize_instances()
        for port in self.ports:
            self.launch_instance(port, chromium_bin)
            time.sleep(1)
