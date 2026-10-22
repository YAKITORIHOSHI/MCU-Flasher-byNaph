using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[assembly: AssemblyTitle("MCU Flasher")]
[assembly: AssemblyDescription("Portable MCU Flasher executable launcher")]
[assembly: AssemblyCompany("Naph")]
[assembly: AssemblyProduct("MCU Flasher by Naph")]
[assembly: AssemblyVersion("6.0.0.0")]
[assembly: ComVisible(false)]

internal static class LauncherFolderWindows
{
    [STAThread]
    private static int Main(string[] arguments)
    {
        try
        {
            string architecture = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITEW6432");
            if (String.IsNullOrEmpty(architecture))
                architecture = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE");
            if (!String.Equals(architecture, "AMD64", StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("This release provides MCU Flasher for Windows AMD64. Native Windows ARM64 support is still being implemented.");
            string launcher = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
            DirectoryInfo folder = new DirectoryInfo(launcher);
            if (!String.Equals(folder.Name, "LAUNCHER", StringComparison.OrdinalIgnoreCase) || folder.Parent == null)
                throw new InvalidOperationException("Keep WINDOWS.exe inside the application's LAUNCHER folder.");
            string root = folder.Parent.FullName;
            string entry = Path.Combine(root, "direct", "windows", "run.vbs");
            string host = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wscript.exe");
            if (!File.Exists(entry) || !File.Exists(host))
                throw new FileNotFoundException("Cannot locate the Windows startup entry. Keep the complete application folder together.");
            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = host;
            // WSH parses only its fixed switch and filesystem script path.
            // User values go through the child's private environment, avoiding
            // WSH quote/percent expansion and retaining empty/Unicode values.
            start.Arguments = "//nologo \"" + entry + "\" --mcu-flasher-wsh-arguments";
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            List<string> stale = new List<string>();
            foreach (string key in start.EnvironmentVariables.Keys)
                if (key.StartsWith("MCU_FLASHER_WSH_ARG_", StringComparison.OrdinalIgnoreCase)) stale.Add(key);
            foreach (string key in stale) start.EnvironmentVariables.Remove(key);
            if (arguments.Length > 1024) throw new ArgumentException("Too many launcher arguments (maximum 1024).");
            int units = 0;
            for (int index = 0; index < arguments.Length; ++index)
            {
                units = checked(units + arguments[index].Length + 1);
                if (arguments[index].Length > 32763 || units > 32767)
                    throw new ArgumentException("Launcher arguments are too long.");
                start.EnvironmentVariables["MCU_FLASHER_WSH_ARG_" + index.ToString(System.Globalization.CultureInfo.InvariantCulture)] = "v1:" + arguments[index];
            }
            start.EnvironmentVariables["MCU_FLASHER_WSH_ARG_COUNT"] = arguments.Length.ToString(System.Globalization.CultureInfo.InvariantCulture);
            using (Process child = Process.Start(start))
            {
                if (child == null) throw new InvalidOperationException("The Windows startup process did not start.");
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "MCU Flasher", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
