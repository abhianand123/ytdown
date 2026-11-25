import os
import sys
import time
import re
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

class SystemTelemetry:
    def __init__(self):
        self.last_sample_time: float = 0.0
        self.last_core_stats: Dict[str, tuple[float, float]] = {}
        self.cached_device_info = self._detect_device_specs()
        # Initialize first CPU baseline
        self._sample_proc_stat()

    def _detect_device_specs(self) -> Dict[str, Any]:
        model = "Unknown Android Device"
        platform = "ARM64 Linux"
        soc = "Qualcomm Snapdragon"

        if os.path.exists("/system/bin/getprop"):
            try:
                m = subprocess.run(["getprop", "ro.product.model"], capture_output=True, text=True, timeout=1).stdout.strip()
                if m: model = m
                p = subprocess.run(["getprop", "ro.board.platform"], capture_output=True, text=True, timeout=1).stdout.strip()
                if p: platform = p
                s = subprocess.run(["getprop", "ro.soc.manufacturer"], capture_output=True, text=True, timeout=1).stdout.strip()
                if s: soc = f"{s} {platform}".strip()
            except Exception:
                pass
        elif sys.platform == "win32":
            model = os.environ.get("COMPUTERNAME", "Windows Host")
            platform = "x86_64 Windows"
            soc = os.environ.get("PROCESSOR_IDENTIFIER", "Intel/AMD")

        cores_count = os.cpu_count() or 8
        kernel = ""
        try:
            kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=1).stdout.strip()
        except Exception:
            kernel = sys.platform

        return {
            "model": model,
            "platform": platform,
            "soc": soc,
            "cores_count": cores_count,
            "kernel": kernel
        }

    def _sample_proc_stat(self) -> Dict[str, tuple[float, float]]:
        cores = {}
        if os.path.exists("/proc/stat"):
            try:
                with open("/proc/stat", "r") as f:
                    for line in f:
                        if line.startswith("cpu"):
                            parts = line.strip().split()
                            name = parts[0]
                            # fields: user, nice, system, idle, iowait, irq, softirq, steal
                            vals = [float(x) for x in parts[1:8]]
                            idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
                            total = sum(vals)
                            cores[name] = (idle, total)
            except Exception:
                pass
        return cores

    def get_cpu_telemetry(self) -> Dict[str, Any]:
        now = time.time()
        current_stats = self._sample_proc_stat()
        
        cpu_usage = {}
        if self.last_core_stats and current_stats and (now - self.last_sample_time) > 0.05:
            for name, (curr_idle, curr_total) in current_stats.items():
                if name in self.last_core_stats:
                    prev_idle, prev_total = self.last_core_stats[name]
                    idle_delta = curr_idle - prev_idle
                    total_delta = curr_total - prev_total
                    if total_delta > 0:
                        pct = (1.0 - (idle_delta / total_delta)) * 100.0
                        cpu_usage[name] = round(max(0.0, min(100.0, pct)), 1)
                    else:
                        cpu_usage[name] = 0.0
        else:
            # Fallback if first read or no delta yet
            time.sleep(0.08)
            second_stats = self._sample_proc_stat()
            for name, (curr_idle, curr_total) in second_stats.items():
                if name in current_stats:
                    prev_idle, prev_total = current_stats[name]
                    idle_delta = curr_idle - prev_idle
                    total_delta = curr_total - prev_total
                    if total_delta > 0:
                        pct = (1.0 - (idle_delta / total_delta)) * 100.0
                        cpu_usage[name] = round(max(0.0, min(100.0, pct)), 1)
            current_stats = second_stats

        self.last_core_stats = current_stats
        self.last_sample_time = now

        overall_pct = cpu_usage.get("cpu", 0.0)

        # Per core frequency & online status
        cores_list = []
        total_cores = self.cached_device_info["cores_count"]
        for i in range(total_cores):
            core_name = f"cpu{i}"
            cur_freq_khz = 0
            max_freq_khz = 2016000
            online = True

            freq_path = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq"
            max_path = f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_max_freq"
            online_path = f"/sys/devices/system/cpu/cpu{i}/online"

            if os.path.exists(online_path):
                try:
                    with open(online_path) as f:
                        online = (f.read().strip() == "1")
                except Exception:
                    online = True

            if os.path.exists(freq_path):
                try:
                    with open(freq_path) as f:
                        cur_freq_khz = int(f.read().strip())
                except Exception:
                    pass

            if os.path.exists(max_path):
                try:
                    with open(max_path) as f:
                        max_freq_khz = int(f.read().strip())
                except Exception:
                    pass

            cores_list.append({
                "core": i,
                "usage_percent": cpu_usage.get(core_name, 0.0),
                "cur_mhz": round(cur_freq_khz / 1000.0),
                "max_mhz": round(max_freq_khz / 1000.0),
                "online": online,
                "cluster": "LITTLE" if i < 4 else "big"
            })

        # Load average
        load_avg = [0.0, 0.0, 0.0]
        if os.path.exists("/proc/loadavg"):
            try:
                with open("/proc/loadavg") as f:
                    parts = f.read().strip().split()
                    load_avg = [float(parts[0]), float(parts[1]), float(parts[2])]
            except Exception:
                pass

        # CPU Temperature
        cpu_temp = self._read_soc_temp()

        return {
            "overall_usage_percent": overall_pct,
            "cores": cores_list,
            "load_average": load_avg,
            "temperature_c": cpu_temp
        }

    def _read_soc_temp(self) -> float:
        """Reads highest relevant CPU / SoC thermal zone temp."""
        thermal_zones = [
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
            "/sys/class/thermal/thermal_zone14/temp",
            "/sys/class/thermal/thermal_zone20/temp"
        ]
        highest = 0.0
        for tz in thermal_zones:
            if os.path.exists(tz):
                try:
                    with open(tz) as f:
                        val = float(f.read().strip())
                        if val > 1000:
                            val = val / 1000.0
                        if 10.0 <= val <= 110.0:
                            highest = max(highest, val)
                except Exception:
                    pass
        return round(highest, 1) if highest > 0 else 34.0

    def get_gpu_telemetry(self) -> Dict[str, Any]:
        """Reads Adreno / Mali GPU busy percentage, clock speed, and model."""
        gpu_busy = 0.0
        gpu_mhz = 0
        gpu_max_mhz = 650
        gpu_model = "Adreno 506"
        available_freqs = []
        is_active = False

        # Qualcomm Adreno KGSL
        kgsl_dir = Path("/sys/class/kgsl/kgsl-3d0")
        if kgsl_dir.exists():
            # Model
            model_file = kgsl_dir / "gpu_model"
            if model_file.exists():
                try:
                    gpu_model = model_file.read_text().strip()
                except Exception:
                    pass

            # Busy percentage
            busy_file = kgsl_dir / "gpu_busy_percentage"
            if busy_file.exists():
                try:
                    raw = busy_file.read_text().strip().replace("%", "").strip()
                    gpu_busy = float(raw)
                except Exception:
                    pass
            elif (kgsl_dir / "gpubusy").exists():
                try:
                    # gpubusy returns: <busy_cycles> <total_cycles>
                    raw = (kgsl_dir / "gpubusy").read_text().strip().split()
                    if len(raw) >= 2 and float(raw[1]) > 0:
                        gpu_busy = round((float(raw[0]) / float(raw[1])) * 100.0, 1)
                except Exception:
                    pass

            # Current Clock
            clk_file = kgsl_dir / "clock_mhz"
            if clk_file.exists():
                try:
                    gpu_mhz = int(clk_file.read_text().strip())
                except Exception:
                    pass
            elif (kgsl_dir / "gpuclk").exists():
                try:
                    raw_clk = int((kgsl_dir / "gpuclk").read_text().strip())
                    gpu_mhz = raw_clk // 1000000 if raw_clk > 10000 else raw_clk
                except Exception:
                    pass

            # Max Clock & Table
            max_clk_file = kgsl_dir / "max_clock_mhz"
            if max_clk_file.exists():
                try:
                    gpu_max_mhz = int(max_clk_file.read_text().strip())
                except Exception:
                    pass

            freq_table_file = kgsl_dir / "gpu_available_frequencies"
            if not freq_table_file.exists():
                freq_table_file = kgsl_dir / "freq_table_mhz"

            if freq_table_file.exists():
                try:
                    raw_freqs = freq_table_file.read_text().strip().split()
                    for f in raw_freqs:
                        val = int(f)
                        if val > 100000:
                            val = val // 1000000
                        available_freqs.append(val)
                    available_freqs.sort()
                    if available_freqs:
                        gpu_max_mhz = max(available_freqs)
                except Exception:
                    pass

            is_active = (gpu_busy > 0.0 or gpu_mhz > 216)

        return {
            "available": kgsl_dir.exists(),
            "model": gpu_model,
            "usage_percent": round(max(0.0, min(100.0, gpu_busy)), 1),
            "clock_mhz": gpu_mhz,
            "max_clock_mhz": gpu_max_mhz,
            "available_freqs_mhz": available_freqs or [133, 216, 320, 400, 510, 650],
            "is_active": is_active
        }

    def get_memory_telemetry(self) -> Dict[str, Any]:
        """Reads RAM and Swap usage from /proc/meminfo."""
        total_kb = 3600000
        avail_kb = 1800000
        free_kb = 400000
        cached_kb = 1200000
        swap_total_kb = 0
        swap_free_kb = 0

        if os.path.exists("/proc/meminfo"):
            try:
                with open("/proc/meminfo") as f:
                    for line in f:
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
            "swap_total_mb": round(swap_total_kb / 1024)
        }

    def get_battery_telemetry(self) -> Dict[str, Any]:
        """Reads battery capacity, temperature, voltage, and charging state."""
        capacity = 100
        temp_c = 28.0
        status = "Discharging"
        voltage_v = 4.0
        current_ma = 0

        bat_dir = Path("/sys/class/power_supply/battery")
        if bat_dir.exists():
            try:
                if (bat_dir / "capacity").exists():
                    capacity = int((bat_dir / "capacity").read_text().strip())
                if (bat_dir / "status").exists():
                    status = (bat_dir / "status").read_text().strip()
                if (bat_dir / "temp").exists():
                    t = float((bat_dir / "temp").read_text().strip())
                    temp_c = round(t / 10.0 if t > 100 else t, 1)
                if (bat_dir / "voltage_now").exists():
                    v = float((bat_dir / "voltage_now").read_text().strip())
                    voltage_v = round(v / 1000000.0 if v > 10000 else v / 1000.0, 2)
                if (bat_dir / "current_now").exists():
                    c = float((bat_dir / "current_now").read_text().strip())
                    current_ma = round(c / 1000.0 if abs(c) > 10000 else c)
            except Exception:
                pass

        return {
            "capacity_percent": capacity,
            "temperature_c": temp_c,
            "status": status,
            "voltage_v": voltage_v,
            "current_ma": current_ma,
            "is_charging": status.lower() in ("charging", "full")
        }

    def get_top_processes(self, limit: int = 7) -> List[Dict[str, Any]]:
        """Extracts top active processes sorted by CPU and memory."""
        proc_list = []
        if os.path.exists("/system/bin/ps") or shutil.which("ps"):
            try:
                res = subprocess.run(
                    ["ps", "-eo", "pid,user,%cpu,%mem,comm", "--sort=-%cpu"],
                    capture_output=True, text=True, timeout=1.5
                )
                if res.returncode == 0:
                    lines = res.stdout.strip().split("\n")[1:] # skip header
                    for line in lines[:limit]:
                        parts = line.split(None, 4)
                        if len(parts) >= 5:
                            proc_list.append({
                                "pid": int(parts[0]),
                                "user": parts[1],
                                "cpu_percent": float(parts[2]),
                                "mem_percent": float(parts[3]),
                                "command": parts[4].strip()
                            })
            except Exception:
                pass
        return proc_list

    def get_full_snapshot(self) -> Dict[str, Any]:
        """Gathers unified real-time system hardware snapshot."""
        uptime_sec = 0
        if os.path.exists("/proc/uptime"):
            try:
                with open("/proc/uptime") as f:
                    uptime_sec = int(float(f.read().split()[0]))
            except Exception:
                pass

        days = uptime_sec // 86400
        hours = (uptime_sec % 86400) // 3600
        mins = (uptime_sec % 3600) // 60
        uptime_str = f"{days}d {hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"

        return {
            "device": {
                **self.cached_device_info,
                "uptime_seconds": uptime_sec,
                "uptime_formatted": uptime_str
            },
            "cpu": self.get_cpu_telemetry(),
            "gpu": self.get_gpu_telemetry(),
            "memory": self.get_memory_telemetry(),
            "battery": self.get_battery_telemetry(),
            "processes": self.get_top_processes(limit=8),
            "timestamp": time.time()
        }

# Global singleton
system_telemetry = SystemTelemetry()

if __name__ == "__main__":
    import json
    data = system_telemetry.get_full_snapshot()
    print(json.dumps(data, indent=2))

