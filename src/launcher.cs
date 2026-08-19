using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows.Forms;

[assembly: AssemblyTitle("MCU Flasher Launcher")]
[assembly: AssemblyDescription("Native Launcher for MCU Flasher by Naph")]
[assembly: AssemblyConfiguration("")]
[assembly: AssemblyCompany("Naph")]
[assembly: AssemblyProduct("MCU Flasher by Naph")]
[assembly: AssemblyCopyright("Copyright © 2026 Naph. All rights reserved.")]
[assembly: AssemblyTrademark("")]
[assembly: AssemblyCulture("")]
[assembly: ComVisible(false)]
[assembly: AssemblyVersion("6.0.0.0")]
[assembly: AssemblyFileVersion("6.0.0.0")]

internal static class MCUFlasherLauncher
{
    [STAThread]
    private static void Main()
    {
        try
        {
            // Dynamically resolve base directory of the running executable
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            if (string.IsNullOrEmpty(baseDir))
            {
                baseDir = Directory.GetCurrentDirectory();
            }

            // Normalise trailing separator
            if (!baseDir.EndsWith(Path.DirectorySeparatorChar.ToString()) &&
                !baseDir.EndsWith(Path.AltDirectorySeparatorChar.ToString()))
            {
                baseDir += Path.DirectorySeparatorChar;
            }

            // Candidate locations for runThisOnWindows.vbs (dynamic relative resolution)
            string[] relativeCandidates = new string[]
            {
                Path.Combine(baseDir, "direct", "runThisOnWindows.vbs"),
                Path.Combine(baseDir, "runThisOnWindows.vbs"),
                Path.Combine(baseDir, "..", "direct", "runThisOnWindows.vbs"),
                Path.Combine(baseDir, "..", "runThisOnWindows.vbs")
            };

            string vbsPath = null;
            string appRoot = baseDir;

            foreach (string candidate in relativeCandidates)
            {
                try
                {
                    string fullPath = Path.GetFullPath(candidate);
                    if (File.Exists(fullPath))
                    {
                        vbsPath = fullPath;
                        string vbsDir = Path.GetDirectoryName(fullPath) ?? baseDir;

                        // Determine project root directory dynamically
                        if (Directory.Exists(Path.Combine(vbsDir, "src", "modules")))
                        {
                            appRoot = vbsDir;
                        }
                        else
                        {
                            string parentDir = Path.GetDirectoryName(vbsDir);
                            if (!string.IsNullOrEmpty(parentDir) &&
                                Directory.Exists(Path.Combine(parentDir, "src", "modules")))
                            {
                                appRoot = parentDir;
                            }
                            else if (Directory.Exists(Path.Combine(baseDir, "src", "modules")))
                            {
                                appRoot = baseDir;
                            }
                            else
                            {
                                appRoot = vbsDir;
                            }
                        }
                        break;
                    }
                }
                catch
                {
                    // Ignore path resolution errors for invalid candidates
                }
            }

            // If not found, display dynamic error message
            if (string.IsNullOrEmpty(vbsPath) || !File.Exists(vbsPath))
            {
                MessageBox.Show(
                    "Could not find 'runThisOnWindows.vbs' in:\n" +
                    baseDir + "\n\n" +
                    "Checked locations:\n" +
                    " • " + Path.Combine(baseDir, "direct", "runThisOnWindows.vbs") + "\n" +
                    " • " + Path.Combine(baseDir, "runThisOnWindows.vbs") + "\n\n" +
                    "Please ensure MCU_Flasher.exe is located inside the MCU Flasher application folder.",
                    "MCU Flasher Launcher",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return;
            }

            // Dynamically locate system wscript.exe
            string systemFolder = Environment.GetFolderPath(Environment.SpecialFolder.System);
            string wscriptPath = Path.Combine(systemFolder, "wscript.exe");
            if (!File.Exists(wscriptPath))
            {
                wscriptPath = "wscript.exe"; // Fallback to PATH resolution
            }

            ProcessStartInfo psi = new ProcessStartInfo
            {
                FileName = wscriptPath,
                Arguments = "\"" + vbsPath + "\"",
                WorkingDirectory = appRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };

            Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show(
                "Failed to launch MCU Flasher:\n\n" + ex.Message,
                "MCU Flasher Launch Error",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
        }
    }
}
