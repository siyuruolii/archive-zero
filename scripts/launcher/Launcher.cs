using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows.Forms;

internal sealed class Launcher : Form {
    readonly TextBox output = new TextBox();
    readonly Label status = new Label();
    readonly string root;
    readonly bool prepareOnly, update;
    readonly int waitPid;
    bool busy = true;
    StreamWriter log;
    Launcher(string directory, bool prepare, bool updating, int waiting) {
        root = directory; prepareOnly = prepare; update = updating; waitPid = waiting;
        Text = "Archive Zero · 启动器"; Width = 700; Height = 440;
        StartPosition = FormStartPosition.CenterScreen; BackColor = Color.FromArgb(239, 247, 252);
        Font = new Font("Microsoft YaHei UI", 10);
        string icon = Path.Combine(root, "desktop/assets/brand/archive-zero.ico");
        if (File.Exists(icon)) Icon = new Icon(icon);
        status.Text = updating ? "正在更新零号档案…" : "正在准备晴空阅览室…";
        status.Dock = DockStyle.Top; status.Height = 70; status.Padding = new Padding(22, 20, 0, 0);
        status.Font = new Font(Font.FontFamily, 15);
        output.Multiline = true; output.ReadOnly = true; output.ScrollBars = ScrollBars.Vertical;
        output.Dock = DockStyle.Fill; output.BackColor = Color.White; output.BorderStyle = BorderStyle.None;
        output.Font = new Font("Consolas", 10);
        var hint = new Label { Text = "首次启动会联网下载环境与依赖，之后会复用。启动器不会自动填写模型密钥。", Dock = DockStyle.Bottom, Height = 45, Padding = new Padding(18, 10, 0, 0) };
        Controls.Add(output); Controls.Add(status); Controls.Add(hint);
        FormClosing += (s,e) => { if (busy) { e.Cancel = true; MessageBox.Show(this, "准备过程中请稍候；完成或出错后可关闭此窗口。", "零号档案"); } };
        Shown += async (s,e) => await Run();
    }
    void Line(string value) {
        if (String.IsNullOrEmpty(value)) return;
        value = Regex.Replace(value, @"(https?://)[^/\s:@]+:[^/\s@]+@", "$1***@");
        if (InvokeRequired) { BeginInvoke(new Action<string>(Line), value); return; }
        output.AppendText(value + Environment.NewLine);
        if (log != null) { log.WriteLine(value); log.Flush(); }
    }
    async Task Run() {
        try {
            if (!File.Exists(Path.Combine(root, "scripts/bootstrap-desktop.ps1")) || !File.Exists(Path.Combine(root, "pyproject.toml"))) throw new Exception("请把启动器留在完整的项目根目录中，不要单独移动 EXE。");
            Directory.CreateDirectory(Path.Combine(root, ".runtime"));
            log = new StreamWriter(Path.Combine(root, ".runtime/launcher.log"), false, new UTF8Encoding(false));
            var info = new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell/v1.0/powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(root, "scripts/bootstrap-desktop.ps1") + "\"" + (prepareOnly ? " -PrepareOnly" : "") + (update ? " -Update" : "") + (waitPid > 0 ? " -WaitForPid " + waitPid : ""),
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            info.EnvironmentVariables.Remove("PSModulePath");
            using (var child = new Process { StartInfo = info }) {
                child.OutputDataReceived += (s,e) => Line(e.Data); child.ErrorDataReceived += (s,e) => Line(e.Data);
                child.Start(); child.BeginOutputReadLine(); child.BeginErrorReadLine();
                await Task.Run(() => child.WaitForExit());
                busy = false;
                if (child.ExitCode != 0) { Environment.ExitCode = 1; status.Text = "准备未完成，请查看下方原因后重试"; return; }
            }
            Close();
        } catch (Exception e) { busy = false; Environment.ExitCode = 1; status.Text = "启动未完成"; Line(e.Message); }
        finally { if (log != null) { log.Dispose(); log = null; } }
    }
    [STAThread]
    static void Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool update = Array.IndexOf(args, "--update") >= 0;
        int wait = 0;
        for (int i=0; i<args.Length-1; i++) {
            if (args[i] == "--project-root") root = Path.GetFullPath(args[++i]);
            else if (args[i] == "--wait-for-pid") Int32.TryParse(args[++i], out wait);
        }
        // An updater runs outside the tracked EXE so Git can replace that file on Windows.
        if (update && Array.IndexOf(args, "--worker") < 0) {
            string worker = Path.Combine(root, ".runtime/update-launcher-" + Guid.NewGuid().ToString("N") + ".exe");
            Directory.CreateDirectory(Path.GetDirectoryName(worker));
            File.Copy(Application.ExecutablePath, worker);
            Process.Start(new ProcessStartInfo(worker, "--worker --update --project-root \"" + root.TrimEnd('\\') + "\" --wait-for-pid " + wait) { UseShellExecute = false });
            return;
        }
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Launcher(root, Array.IndexOf(args, "--prepare-only") >= 0, update, wait));
    }
}
