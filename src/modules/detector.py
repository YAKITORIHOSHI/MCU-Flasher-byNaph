import subprocess

def get_chassis_types():
    try:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", 
               "Get-CimInstance -ClassName Win32_SystemEnclosure | Select-Object -ExpandProperty ChassisTypes"]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        if result.returncode == 0:
            types = []
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    types.append(int(line))
            return types
    except Exception:
        pass
    return []

def is_laptop():
    laptop_types = {8, 9, 10, 14}
    chassis_types = get_chassis_types()
    if chassis_types:
        return any(t in laptop_types for t in chassis_types)
    
    # Fallback 1: check Linux DMI chassis / battery info
    try:
        import os
        if os.path.exists("/sys/class/dmi/id/chassis_type"):
            with open("/sys/class/dmi/id/chassis_type", "r") as f:
                chassis = f.read().strip()
                if chassis.isdigit() and int(chassis) in laptop_types:
                    return True
        if os.path.exists("/sys/class/power_supply"):
            for supply in os.listdir("/sys/class/power_supply"):
                if supply.startswith("BAT"):
                    return True
    except Exception:
        pass

    # Fallback 2: check battery status using ctypes (Windows)
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ('ACLineStatus', wintypes.BYTE),
                ('BatteryFlag', wintypes.BYTE),
                ('BatteryLifePercent', wintypes.BYTE),
                ('SystemStatus', wintypes.BYTE),
                ('BatteryLifeTime', wintypes.DWORD),
                ('BatteryFullLifeTime', wintypes.DWORD),
            ]

        status = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            # 128 means no battery, 255 means unknown status (typically desktop/no battery)
            if status.BatteryFlag != 128 and status.BatteryFlag != 255:
                return True
    except Exception:
        pass
    
    return False

if __name__ == "__main__":
    print("Laptop" if is_laptop() else "Desktop")