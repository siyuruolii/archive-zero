using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

internal sealed class Launcher {
    readonly Window window;
    readonly TextBox output;
    readonly TextBlock phase, headline;
    readonly Button close, details;
    readonly FrameworkElement detailsPanel;
    readonly string root;
    readonly bool prepareOnly, update;
    readonly int waitPid;
    bool busy = true;
    StreamWriter log;
    Launcher(string directory, bool prepare, bool updating, int waiting, string preview) {
        root = directory; prepareOnly = prepare; update = updating; waitPid = waiting;
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("Launcher.xaml")) window = (Window)XamlReader.Load(stream);
        output = (TextBox)window.FindName("LogOutput"); phase = (TextBlock)window.FindName("PhaseLabel"); headline = (TextBlock)window.FindName("Headline");
        close = (Button)window.FindName("CloseButton"); details = (Button)window.FindName("DetailsButton"); detailsPanel = (FrameworkElement)window.FindName("DetailsPanel");
        LoadImage("BrandImage", "archive-zero-icon.png"); LoadImage("CharacterImage", "sky-archivist-launcher.png");
        string icon = Path.Combine(root, "desktop/assets/brand/archive-zero-icon.png"); if (File.Exists(icon)) window.Icon = new BitmapImage(new Uri(icon));
        string config = Path.Combine(root, ".development.json");
        if (File.Exists(config)) ((TextBlock)window.FindName("ModeLabel")).Text = "开发工作区 · 独立测试资料";
        if (updating) { headline.Text = "整理新的线索，\n即将重新见面。"; phase.Text = "正在等待应用安全退出…"; }
        ((Button)window.FindName("MinimizeButton")).Click += (s,e) => window.WindowState = WindowState.Minimized;
        close.Click += (s,e) => window.Close(); details.Click += (s,e) => ToggleDetails();
        ((FrameworkElement)window.FindName("DragHeader")).MouseLeftButtonDown += (s,e) => { if (e.LeftButton == MouseButtonState.Pressed) { try { window.DragMove(); } catch {} } };
        window.Closing += (s,e) => { if (busy) { e.Cancel = true; phase.Text = "正在准备，可先最小化窗口，完成后会自动打开。"; } };
        window.Loaded += async (s,e) => {
            if (preview != null) {
                await window.Dispatcher.InvokeAsync(() => {}, DispatcherPriority.ApplicationIdle);
                var bitmap = new RenderTargetBitmap((int)window.ActualWidth, (int)window.ActualHeight, 96, 96, PixelFormats.Pbgra32); bitmap.Render(window);
                var png = new PngBitmapEncoder(); png.Frames.Add(BitmapFrame.Create(bitmap)); using (var file = File.Create(preview)) png.Save(file);
                busy = false; window.Close();
            } else await Run();
        };
    }
    void LoadImage(string control, string file) {
        string path = Path.Combine(root, "desktop/assets/brand", file);
        if (File.Exists(path)) ((Image)window.FindName(control)).Source = new BitmapImage(new Uri(path));
    }
    void ToggleDetails() {
        bool show = detailsPanel.Visibility != Visibility.Visible; detailsPanel.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        window.Height = Math.Min(show ? 650 : 510, SystemParameters.WorkArea.Height - 20); details.Content = show ? "收起详细日志  ⌃" : "查看详细日志  ⌄";
    }
    void Line(string value) {
        if (String.IsNullOrEmpty(value)) return;
        value = Regex.Replace(value, @"(https?://)[^/\s:@]+:[^/\s@]+@", "$1***@");
        if (!window.Dispatcher.CheckAccess()) { window.Dispatcher.BeginInvoke(new Action<string>(Line), value); return; }
        output.AppendText(value + Environment.NewLine); output.ScrollToEnd(); if (log != null) { log.WriteLine(value); log.Flush(); }
        int step = 0;
        if (value.StartsWith("Preparing ") || value.StartsWith("Creating local")) { phase.Text = "正在准备独立运行环境…"; step = 1; }
        if (value.StartsWith("Installing ") || value.StartsWith("Downloading Electron")) { phase.Text = "正在准备依赖，首次启动请稍候…"; step = 2; }
        if (value.StartsWith("Building ")) { phase.Text = "正在整理阅览室界面…"; step = 3; }
        if (value.StartsWith("Opening ") || value.StartsWith("Preparation complete")) { phase.Text = "准备就绪，即将打开阅览室…"; step = 3; }
        if (value.StartsWith("Checking repository")) phase.Text = "正在检查并获取代码更新…";
        if (step > 0) for (int i=1;i<=3;i++) ((TextBlock)window.FindName(i==1 ? "StepOne" : i==2 ? "StepTwo" : "StepThree")).Foreground = new SolidColorBrush(i<=step ? Color.FromRgb(57,117,171) : Color.FromRgb(142,164,179));
    }
    void Failed(string message) {
        busy = false; close.IsEnabled = true; Environment.ExitCode = 1; headline.Text = "暂时没能打开，\n线索都还在。"; phase.Text = "请查看详细原因，处理后重新启动。";
        ((ProgressBar)window.FindName("Progress")).IsIndeterminate = false; if (message != null) Line(message); if (detailsPanel.Visibility != Visibility.Visible) ToggleDetails();
    }
    async Task Run() {
        try {
            if (!File.Exists(Path.Combine(root, "scripts/bootstrap-desktop.ps1")) || !File.Exists(Path.Combine(root, "pyproject.toml"))) throw new Exception("请把启动器留在完整的项目根目录中，不要单独移动 EXE。");
            string logs = Path.Combine(root, File.Exists(Path.Combine(root, ".development.json")) ? ".dev-data/launcher" : ".runtime"); Directory.CreateDirectory(logs); log = new StreamWriter(Path.Combine(logs, "launcher.log"), false, new UTF8Encoding(false));
            var info = new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell/v1.0/powershell.exe"),
                Arguments = "-NoProfile -ExecutionPolicy Bypass -File \"" + Path.Combine(root, "scripts/bootstrap-desktop.ps1") + "\"" + (prepareOnly ? " -PrepareOnly" : "") + (update ? " -Update" : "") + (waitPid > 0 ? " -WaitForPid " + waitPid : ""),
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true, StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            info.EnvironmentVariables.Remove("PSModulePath");
            using (var child = new Process { StartInfo = info }) {
                child.OutputDataReceived += (s,e) => Line(e.Data); child.ErrorDataReceived += (s,e) => Line(e.Data);
                child.Start(); child.BeginOutputReadLine(); child.BeginErrorReadLine(); await Task.Run(() => child.WaitForExit());
                if (child.ExitCode != 0) { Failed(null); return; }
            }
            busy = false; window.Close();
        } catch (Exception e) { Failed(e.Message); } finally { if (log != null) { log.Dispose(); log = null; } }
    }
    [STAThread]
    static void Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory, preview = null; bool update = Array.IndexOf(args, "--update") >= 0; int wait = 0;
        for (int i=0; i<args.Length-1; i++) { if (args[i] == "--project-root") root = Path.GetFullPath(args[++i]); else if (args[i] == "--wait-for-pid") Int32.TryParse(args[++i], out wait); else if (args[i] == "--preview") preview = Path.GetFullPath(args[++i]); }
        if (update && Array.IndexOf(args, "--worker") < 0) {
            string worker = Path.Combine(root, ".runtime/update-launcher-" + Guid.NewGuid().ToString("N") + ".exe"); Directory.CreateDirectory(Path.GetDirectoryName(worker)); File.Copy(Assembly.GetExecutingAssembly().Location, worker);
            Process.Start(new ProcessStartInfo(worker, "--worker --update --project-root \"" + root.TrimEnd('\\') + "\" --wait-for-pid " + wait) { UseShellExecute = false }); return;
        }
        var app = new Application(); var launcher = new Launcher(root, Array.IndexOf(args, "--prepare-only") >= 0, update, wait, preview); app.Run(launcher.window);
    }
}
