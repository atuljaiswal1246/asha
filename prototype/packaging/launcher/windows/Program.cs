// Asha — Windows launcher for the bundled app.
// Runs the embedded Python runtime with the app code and shows the local UI in
// an embedded WebView2 window. Everything a developer needs is bundled; voice
// models point at the app's models\ folder, so nothing downloads at runtime.
// OmniRoute is NOT bundled (optional user-installed add-on); Asha reaches a
// user's own OmniRoute through the app's OMNIROUTE_BASE_URL setting.

using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
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

        string root = AppContext.BaseDirectory;
        LogKeylessConfig(root);
        StartBackend(root);

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
            try { await web.EnsureCoreWebView2Async(null); web.CoreWebView2.Navigate(Url); }
            catch (Exception ex)
            {
                MessageBox.Show("WebView2 failed to start:\n" + ex.Message +
                    "\n\nInstall the WebView2 Runtime, then retry.", "Asha");
            }
        };
        web.NavigationCompleted += async (_, e) =>
        {
            if (e.IsSuccess) return;
            await System.Threading.Tasks.Task.Delay(1500);
            try { web.CoreWebView2?.Navigate(Url); } catch { }
        };
        form.FormClosed += (_, _) => { StopBackend(); };
        Application.Run(form);
        StopBackend();
    }

    // Keyless-config attestation. Reads the shipped app\.env (the exact file
    // the server loads) and states which non-secret settings it carries, plus
    // whether any local provider key is present. Only allow-listed non-secret
    // values are echoed; any credential-shaped value is reported by NAME only,
    // never its value. Written to <LocalAppData>\Jarvis\logs\startup.log so a
    // support call can answer "is this build keyless?" without reading files.
    private static readonly string[] NonSecretSettings =
        { "JARVIS_BRAIN_MODEL", "JARVIS_BRAIN_TRANSPORT", "OMNIROUTE_BASE_URL", "JARVIS_GATEWAY_URL", "JARVIS_DEMO_SIGNUP" };
    private const string KeyShape =
        @"sk-[A-Za-z0-9_-]{16,}|sk-or-v1-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{30,}|GOCSPX-[A-Za-z0-9_-]{16,}|figd_[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9_-]{20,}|xai-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----";

    private static void LogKeylessConfig(string root)
    {
        string line;
        string envPath = Path.Combine(root, "app", ".env");
        if (!File.Exists(envPath))
        {
            line = "[config] keyless build: shipped config absent; defaults only; no provider API keys bundled";
        }
        else
        {
            var settings = new System.Collections.Generic.List<string>();
            var credentials = new System.Collections.Generic.List<string>();
            foreach (string raw in File.ReadAllLines(envPath))
            {
                string t = raw.Trim();
                if (t.Length == 0 || t.StartsWith("#")) continue;
                int eq = t.IndexOf('=');
                if (eq <= 0) continue;
                string key = t.Substring(0, eq).Trim();
                string value = t.Substring(eq + 1).Trim();
                bool shaped = value.Length > 0 &&
                    System.Text.RegularExpressions.Regex.IsMatch(value, KeyShape);
                if (shaped || key.EndsWith("API_KEY") || key.EndsWith("SECRET") || key.EndsWith("_TOKEN"))
                    credentials.Add(key);
                else if (Array.IndexOf(NonSecretSettings, key) >= 0)
                    settings.Add(key + "=" + value);
                else
                    settings.Add(key);
            }
            string summary = settings.Count == 0 ? "(none)" : string.Join(", ", settings);
            line = credentials.Count == 0
                ? "[config] keyless build: no provider API keys in app\\.env; loaded: " + summary
                : "[config] shipped app\\.env has user-supplied credential(s) [" + string.Join(", ", credentials) + "] (values never logged); loaded: " + summary;
        }
        Debug.WriteLine(line);
        try
        {
            string dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jarvis", "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "startup.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + line + Environment.NewLine);
        }
        catch { }
    }

    private static void StartBackend(string root)
    {
        string py = Path.Combine(root, "runtime", "python.exe");
        string uiDir = Path.Combine(root, "app", "ui");
        string models = Path.Combine(root, "models");
        if (!File.Exists(py))
        {
            MessageBox.Show("Asha is missing its bundled runtime at:\n" + py, "Asha");
            return;
        }
        var psi = new ProcessStartInfo
        {
            FileName = py,
            Arguments = "\"" + Path.Combine(uiDir, "launch.py") + "\" --no-browser",
            WorkingDirectory = uiDir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.Environment["KOKORO_MODEL_PATH"] = Path.Combine(models, "kokoro", "kokoro-v1.0.onnx");
        psi.Environment["KOKORO_VOICES_PATH"] = Path.Combine(models, "kokoro", "voices-v1.0.bin");
        psi.Environment["MOONSHINE_VOICE_CACHE"] = Path.Combine(models, "moonshine");
        // Keep runtime data out of the install dir; default project = %USERPROFILE%\Jarvis.
        psi.Environment["JARVIS_DATA_DIR"] =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Jarvis");
        psi.Environment["JARVIS_DEFAULT_PROJECT"] =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "Jarvis");
        psi.Environment["PYTHONUNBUFFERED"] = "1";
        try { _backend = Process.Start(psi); }
        catch (Exception ex) { MessageBox.Show("Could not start Asha:\n" + ex.Message, "Asha"); }
    }

    private static void StopBackend()
    {
        try { if (_backend is { HasExited: false }) _backend.Kill(entireProcessTree: true); }
        catch { }
        _backend = null;
    }
}
