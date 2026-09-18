// Asha — Windows desktop shell (WebView2).
//
// Starts the local Python backend (prototype/ui/launch.py) and shows the UI in
// an embedded WebView2 window — the Windows twin of the macOS WKWebView app.
//
// Build:  build.bat   (needs the .NET 8 SDK; WebView2 runtime ships with Win10/11)
// Run:    dist\Asha.exe  — or the no-build fallback start_asha.bat (Edge app mode).

using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Asha;

internal static class Program
{
    private static Process? _backend;
    private const string Url = "http://127.0.0.1:8000/";

    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        var repo = FindRepoRoot();
        if (repo == null)
        {
            MessageBox.Show(
                "Could not find the Asha repo.\n\nSet JARVIS_HOME to the folder that " +
                "contains prototype/ui/server.py and run again.",
                "Asha", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        StartBackend(repo);

        var form = new Form
        {
            Text = "Asha",
            Width = 1360,
            Height = 900,
            MinimumSize = new System.Drawing.Size(900, 600),
            StartPosition = FormStartPosition.CenterScreen,
            BackColor = System.Drawing.Color.FromArgb(8, 10, 14),
        };
        var web = new WebView2 { Dock = DockStyle.Fill };
        form.Controls.Add(web);

        form.Load += async (_, _) =>
        {
            try
            {
                await web.EnsureCoreWebView2Async(null);
                web.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                web.CoreWebView2.Settings.IsStatusBarEnabled = false;
                web.CoreWebView2.Navigate(Url);
            }
            catch (Exception ex)
            {
                MessageBox.Show("WebView2 failed to start:\n" + ex.Message +
                                "\n\nInstall the WebView2 Runtime, then retry.",
                                "Asha", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        };

        // The backend can take a few seconds — retry navigation until it answers.
        web.NavigationCompleted += async (_, e) =>
        {
            if (e.IsSuccess) return;
            await System.Threading.Tasks.Task.Delay(1500);
            try { web.CoreWebView2?.Navigate(Url); } catch { /* closing */ }
        };

        form.FormClosed += (_, _) => StopBackend();
        Application.Run(form);
        StopBackend();
    }

    private static string? FindRepoRoot()
    {
        var env = Environment.GetEnvironmentVariable("JARVIS_HOME");
        if (!string.IsNullOrWhiteSpace(env) && File.Exists(Path.Combine(env, "prototype", "ui", "server.py")))
            return env;

        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        for (int i = 0; i < 8 && dir != null; i++, dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "prototype", "ui", "server.py")))
                return dir.FullName;
        }
        return null;
    }

    private static void StartBackend(string repo)
    {
        var ui = Path.Combine(repo, "prototype", "ui");
        var python = Path.Combine(repo, ".venv", "Scripts", "python.exe");
        if (!File.Exists(python)) python = "python";

        var psi = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "launch.py --no-browser",
            WorkingDirectory = ui,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            _backend = Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Could not start the Asha backend:\n" + ex.Message +
                            "\n\nIs Python installed and the venv set up? See winapp/README.md.",
                            "Asha", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static void StopBackend()
    {
        try
        {
            if (_backend is { HasExited: false })
                _backend.Kill(entireProcessTree: true);
        }
        catch { /* best effort */ }
        _backend = null;
    }
}
