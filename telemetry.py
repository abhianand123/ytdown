import ctypes
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None


class SystemTelemetry:
    def __init__(self) -> None:
        self.is_windows = (os.name == "nt")
        self.last_sample_time: float = 0.0
        self.last_core_stats: Dict[str, Tuple[float, float]] = {}
        self.cached_device_info = self._detect_device_specs()

        # On-demand Windows GPU counter cache (only active while /dashboard is open)
        self._gpu_lock = threading.Lock()
        self._cached_gpu_usage = 0.0
        self._last_gpu_poll_req = 0.0
        self._gpu_sampler_running = False

        if self.is_windows and psutil is not None:
            try:
                psutil.cpu_percent(interval=None, percpu=True)
            except Exception:
                pass
        else:
            self._sample_proc_stat()

    def _detect_device_specs(self) -> Dict[str, Any]:
        model = os.environ.get("COMPUTERNAME", platform.node() or "Laptop Host")
        plat = f"{platform.system()} {platform.release()} ({platform.machine()})"
        soc = platform.processor() or platform.machine()
        kernel = platform.version()

        if self.is_windows:
            try:
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
                ) as k:
                    cpu_brand = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
                    if cpu_brand:
                        soc = " ".join(cpu_brand.split())
            except Exception:
                pass
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).Model"],
                    capture_output=True, text=True, timeout=3,
                )
                hw_model = res.stdout.strip()
                if hw_model:
                    model = hw_model
            except Exception:
                pass
        elif os.path.exists("/system/bin/getprop"):
            try:
                m = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=1).stdout.strip()
                if m:
                    model = m
                p = subprocess.run(["getprop", "ro.board.platform"], capture_output=True, text=True, timeout=1).stdout.strip()
                if p:
                    plat = p
                s = subprocess.run(["getprop", "ro.soc.manufacturer"], capture_output=True, text=True, timeout=1).stdout.strip()
                if s:
                    soc = f"{s} {plat}".strip()
            except Exception:
                pass

        if not self.is_windows:
            try:
                kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=1).stdout.strip() or kernel
            except Exception:
                pass

        return {
            "model": model,
            "platform": plat,
            "soc": soc,
            "cores_count": os.cpu_count() or 4,
            "kernel": kernel,
        }

    def _read_win_gpu_name(self) -> str:
        if not self.is_windows:
            return "Integrated GPU"
        try:
            import winreg
            for idx in ("0000", "0001"):
                try:
                    with winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        rf"SYSTEM\CurrentControlSet\Control\Class\{{4d36e968-e325-11ce-bfc1-08002be10318}}\{idx}",
                    ) as g:
                        desc = str(winreg.QueryValueEx(g, "DriverDesc")[0]).strip()
                        if desc:
                            return desc
                except OSError:
                    continue
        except Exception:
            pass
        return "Windows Graphics Adapter"

    def _start_win_gpu_sampler(self) -> None:
        with self._gpu_lock:
            self._last_gpu_poll_req = time.time()
            if self._gpu_sampler_running:
                return
            self._gpu_sampler_running = True

        def _sampler():
            try:
                while (time.time() - self._last_gpu_poll_req) < 20.0:
                    try:
                        res = subprocess.run(
                            ["typeperf", r"\GPU Engine(*engtype_3D)\Utilization Percentage", "-sc", "1"],
                            capture_output=True, text=True, timeout=4,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        if res.returncode == 0:
                            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip() and not ln.startswith('"(PDH-CSV')]
                            total = 0.0
                            for ln in reversed(lines):
                                if ln.startswith('"'):
                                    parts = [p.strip().strip('"') for p in ln.split(",")]
                                    for v in parts[1:]:
                                        try:
                                            total += float(v)
                                        except ValueError:
                                            pass
                                    break
                            with self._gpu_lock:
                                self._cached_gpu_usage = round(max(0.0, min(100.0, total)), 1)
                    except Exception:
                        pass
                    time.sleep(2.0)
            finally:
                with self._gpu_lock:
                    self._gpu_sampler_running = False

        threading.Thread(target=_sampler, daemon=True).start()

    def _sample_proc_stat(self) -> Dict[str, Tuple[float, float]]:
        cores = {}
        if os.path.exists("/proc/stat"):
            try:
                with open("/proc/stat", "r") as f:
                    for line in f:
                        if line.startswith("cpu"):
                            parts = line.strip().split()
                            name = parts[0]
                            vals = [float(x) for x in parts[1:8]]
                            if len(vals) >= 4:
                                idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
                                total = sum(vals)
                                cores[name] = (idle, total)
            except Exception:
                pass
        return cores

    def get_cpu_telemetry(self) -> Dict[str, Any]:
        if self.is_windows:
            return self._get_win_cpu_telemetry()
        return self._get_linux_cpu_telemetry()

    def _get_win_cpu_telemetry(self) -> Dict[str, Any]:
        total_cores = self.cached_device_info["cores_count"]
        overall_pct = 0.0
        per_core_pct = [0.0] * total_cores
        cur_mhz = 0
        max_mhz = 0

        if psutil is not None:
            try:
                per_core_pct = psutil.cpu_percent(interval=None, percpu=True)
                overall_pct = round(sum(per_core_pct) / max(1, len(per_core_pct)), 1)
                freq = psutil.cpu_freq()
                if freq:
                    cur_mhz = round(freq.current)
                    max_mhz = round(freq.max or freq.current)
            except Exception:
                pass

        cores_list = []
        for i in range(len(per_core_pct)):
            cores_list.append({
                "core": i,
                "usage_percent": round(float(per_core_pct[i]), 1),
                "cur_mhz": cur_mhz,
                "max_mhz": max_mhz,
                "online": True,
                "cluster": f"T{i}",
            })

        return {
            "overall_usage_percent": overall_pct,
            "cores": cores_list,
            "load_average": [round(overall_pct / 100.0 * total_cores, 2), 0.0, 0.0],
            "temperature_c": None,
        }

    def _get_linux_cpu_telemetry(self) -> Dict[str, Any]:
        now = time.time()
        current_stats = self._sample_proc_stat()
        cpu_usage: Dict[str, float] = {}
        if self.last_core_stats and current_stats and (now - self.last_sample_time) > 0.05:
            for name, (curr_idle, curr_total) in current_stats.items():
                if name in self.last_core_stats:
                    prev_idle, prev_total = self.last_core_stats[name]
                    idle_delta = curr_idle - prev_idle
                    total_delta = curr_total - prev_total
                    cpu_usage[name] = round(max(0.0, min(100.0, (1.0 - (idle_delta / total_delta)) * 100.0)), 1) if total_delta > 0 else 0.0
        else:
            time.sleep(0.08)
            second_stats = self._sample_proc_stat()
            for name, (curr_idle, curr_total) in second_stats.items():
                if name in current_stats:
                    prev_idle, prev_total = current_stats[name]
                    idle_delta = curr_idle - prev_idle
                    total_delta = curr_total - prev_total
                    if total_delta > 0:
                        cpu_usage[name] = round(max(0.0, min(100.0, (1.0 - (idle_delta / total_delta)) * 100.0)), 1)
            current_stats = second_stats

        self.last_core_stats = current_stats
        self.last_sample_time = now
        overall_pct = cpu_usage.get("cpu", 0.0)

        cores_list = []
        total_cores = self.cached_device_info["cores_count"]
        for i in range(total_cores):
            cur_freq_khz = 0
            max_freq_khz = 0
            online = True
            online_path = f"/sys/devices/system/cpu/cpu{i}/online"
            freq_path = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq"
            max_path = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq"
            if os.path.exists(online_path):
                try:
                    online = (Path(online_path).read_text().strip() == "1")
                except Exception:
                    pass
            if os.path.exists(freq_path):
                try:
                    cur_freq_khz = int(Path(freq_path).read_text().strip())
                except Exception:
                    pass
            if os.path.exists(max_path):
                try:
                    max_freq_khz = int(Path(max_path).read_text().strip())
                except Exception:
                    pass
            cores_list.append({
                "core": i,
                "usage_percent": cpu_usage.get(f"cpu{i}", 0.0),
                "cur_mhz": round(cur_freq_khz / 1000.0),
                "max_mhz": round(max_freq_khz / 1000.0),
                "online": online,
                "cluster": f"C{i}",
            })

        load_avg = [0.0, 0.0, 0.0]
        if os.path.exists("/proc/loadavg"):
            try:
                parts = Path("/proc/loadavg").read_text().strip().split()
                load_avg = [float(parts[0]), float(parts[1]), float(parts[2])]
            except Exception:
                pass

        return {
            "overall_usage_percent": overall_pct,
            "cores": cores_list,
            "load_average": load_avg,
            "temperature_c": self._read_soc_temp(),
        }

    def _read_soc_temp(self) -> Optional[float]:
        highest = 0.0
        for tz in Path("/sys/class/thermal").glob("thermal_zone*/temp") if Path("/sys/class/thermal").exists() else []:
            try:
                val = float(tz.read_text().strip())
                if val > 1000:
                    val /= 1000.0
                if 10.0 <= val <= 115.0:
                    highest = max(highest, val)
            except Exception:
                pass
        return round(highest, 1) if highest > 0 else None

    def get_gpu_telemetry(self) -> Dict[str, Any]:
        if self.is_windows:
            self._start_win_gpu_sampler()
            with self._gpu_lock:
                usage = self._cached_gpu_usage
            model = self.cached_device_info.setdefault("_gpu_model", self._read_win_gpu_name())
            return {
                "available": True,
                "model": model,
                "usage_percent": usage,
                "clock_mhz": None,
                "max_clock_mhz": None,
                "available_freqs_mhz": [],
                "is_active": usage > 1.0,
            }

        gpu_busy = 0.0
        gpu_mhz = 0
        gpu_max_mhz = 0
        gpu_model = "System GPU"
        available_freqs: List[int] = []
        kgsl_dir = Path("/sys/class/kgsl/kgsl-3d0")
        if kgsl_dir.exists():
            if (kgsl_dir / "gpu_model").exists():
                try:
                    gpu_model = (kgsl_dir / "gpu_model").read_text().strip()
                except Exception:
                    pass
            if (kgsl_dir / "gpu_busy_percentage").exists():
                try:
                    gpu_busy = float((kgsl_dir / "gpu_busy_percentage").read_text().strip().replace("%", ""))
                except Exception:
                    pass
            if (kgsl_dir / "clock_mhz").exists():
                try:
                    gpu_mhz = int((kgsl_dir / "clock_mhz").read_text().strip())
                except Exception:
                    pass
            if (kgsl_dir / "max_clock_mhz").exists():
                try:
                    gpu_max_mhz = int((kgsl_dir / "max_clock_mhz").read_text().strip())
                except Exception:
                    pass
        return {
            "available": kgsl_dir.exists(),
            "model": gpu_model,
            "usage_percent": round(max(0.0, min(100.0, gpu_busy)), 1),
            "clock_mhz": gpu_mhz,
            "max_clock_mhz": gpu_max_mhz,
            "available_freqs_mhz": available_freqs,
            "is_active": gpu_busy > 0.0,
        }

    def get_memory_telemetry(self) -> Dict[str, Any]:
        if self.is_windows and psutil is not None:
            try:
                vm = psutil.virtual_memory()
                sm = psutil.swap_memory()
                return {
                    "total_mb": round(vm.total / (1024 * 1024)),
                    "used_mb": round(vm.used / (1024 * 1024)),
                    "available_mb": round(vm.available / (1024 * 1024)),
                    "free_mb": round(vm.free / (1024 * 1024)),
                    "cached_mb": round(max(0, vm.total - vm.used - vm.available) / (1024 * 1024)),
                    "usage_percent": round(vm.percent, 1),
                    "swap_used_mb": round(sm.used / (1024 * 1024)),
                    "swap_total_mb": round(sm.total / (1024 * 1024)),
                }
            except Exception:
                pass

        if self.is_windows:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            total_mb = round(st.ullTotalPhys / (1024 * 1024))
            avail_mb = round(st.ullAvailPhys / (1024 * 1024))
            used_mb = max(0, total_mb - avail_mb)
            return {
                "total_mb": total_mb,
                "used_mb": used_mb,
                "available_mb": avail_mb,
                "free_mb": avail_mb,
                "cached_mb": 0,
                "usage_percent": float(st.dwMemoryLoad),
                "swap_used_mb": 0,
                "swap_total_mb": 0,
            }

        total_kb = avail_kb = free_kb = cached_kb = swap_total_kb = swap_free_kb = 0
        if os.path.exists("/proc/meminfo"):
            try:
                for line in Path("/proc/meminfo").read_text().splitlines():
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
                    elif line.startswith("MemFree:"):
                        free_kb = int(line.split()[1])
                    elif line.startswith("Cached:"):
                        cached_kb = int(line.split()[1])
                    elif line.startswith("SwapTotal:"):
                        swap_total_kb = int(line.split()[1])
                    elif line.startswith("SwapFree:"):
                        swap_free_kb = int(line.split()[1])
            except Exception:
                pass
        used_kb = max(0, total_kb - avail_kb)
        usage_pct = round((used_kb / total_kb) * 100.0, 1) if total_kb > 0 else 0.0
        return {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "available_mb": round(avail_kb / 1024),
            "free_mb": round(free_kb / 1024),
            "cached_mb": round(cached_kb / 1024),
            "usage_percent": usage_pct,
            "swap_used_mb": round((swap_total_kb - swap_free_kb) / 1024) if swap_total_kb > 0 else 0,
            "swap_total_mb": round(swap_total_kb / 1024),
        }

    def get_battery_telemetry(self) -> Dict[str, Any]:
        if self.is_windows and psutil is not None:
            try:
                bat = psutil.sensors_battery()
                if bat is not None:
                    plugged = bool(bat.power_plugged)
                    return {
                        "capacity_percent": int(round(bat.percent)),
                        "temperature_c": None,
                        "status": "AC Power / Charging" if plugged else "On Battery",
                        "voltage_v": None,
                        "current_ma": None,
                        "is_charging": plugged,
                    }
            except Exception:
                pass
        if os.path.exists("/sys/class/power_supply/battery"):
            bat_dir = Path("/sys/class/power_supply/battery")
            cap, temp_c, status = 100, None, "Unknown"
            try:
                if (bat_dir / "capacity").exists():
                    cap = int((bat_dir / "capacity").read_text().strip())
                if (bat_dir / "status").exists():
                    status = (bat_dir / "status").read_text().strip()
                if (bat_dir / "temp").exists():
                    t = float((bat_dir / "temp").read_text().strip())
                    temp_c = round(t / 10.0 if t > 100 else t, 1)
            except Exception:
                pass
            return {
                "capacity_percent": cap,
                "temperature_c": temp_c,
                "status": status,
                "voltage_v": None,
                "current_ma": None,
                "is_charging": status.lower() in ("charging", "full"),
            }
        return {
            "capacity_percent": 100,
            "temperature_c": None,
            "status": "AC Power",
            "voltage_v": None,
            "current_ma": None,
            "is_charging": True,
        }

    def get_top_processes(self, limit: int = 8) -> List[Dict[str, Any]]:
        proc_list: List[Dict[str, Any]] = []
        if psutil is not None:
            try:
                items = []
                n_cpus = max(1, os.cpu_count() or 1)
                for proc in psutil.process_iter(["pid", "cpu_percent", "memory_percent", "name"]):
                    info = proc.info
                    pid = info.get("pid") or 0
                    if pid == 0:
                        continue
                    name = (info.get("name") or "").strip()
                    if not name or name.lower() == "system idle process":
                        continue
                    cpu_val = float(info.get("cpu_percent") or 0.0) / n_cpus
                    mem_val = float(info.get("memory_percent") or 0.0)
                    items.append((cpu_val, mem_val, pid, name, proc))
                items.sort(key=lambda x: (x[0], x[1]), reverse=True)
                for cpu_val, mem_val, pid, name, proc in items[:limit]:
                    user = "local"
                    try:
                        raw_user = proc.username()
                        if raw_user:
                            user = raw_user.split("\\")[-1]
                    except Exception:
                        pass
                    proc_list.append({
                        "pid": int(pid),
                        "user": user,
                        "cpu_percent": round(cpu_val, 1),
                        "mem_percent": round(mem_val, 1),
                        "command": name,
                    })
                return proc_list
            except Exception:
                pass
        return proc_list

    def get_full_snapshot(self) -> Dict[str, Any]:
        uptime_sec = 0
        if psutil is not None:
            try:
                uptime_sec = int(max(0, time.time() - psutil.boot_time()))
            except Exception:
                pass
        elif self.is_windows:
            try:
                uptime_sec = int(ctypes.windll.kernel32.GetTickCount64() // 1000)
            except Exception:
                pass
        elif os.path.exists("/proc/uptime"):
            try:
                uptime_sec = int(float(Path("/proc/uptime").read_text().split()[0]))
            except Exception:
                pass

        days = uptime_sec // 86400
        hours = (uptime_sec % 86400) // 3600
        mins = (uptime_sec % 3600) // 60
        uptime_str = f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"

        return {
            "device": {
                **{k: v for k, v in self.cached_device_info.items() if not k.startswith("_")},
                "uptime_seconds": uptime_sec,
                "uptime_formatted": uptime_str,
            },
            "cpu": self.get_cpu_telemetry(),
            "gpu": self.get_gpu_telemetry(),
            "memory": self.get_memory_telemetry(),
            "battery": self.get_battery_telemetry(),
            "processes": self.get_top_processes(limit=8),
            "timestamp": time.time(),
        }


system_telemetry = SystemTelemetry()

if __name__ == "__main__":
    import json
    print(json.dumps(system_telemetry.get_full_snapshot(), indent=2))
