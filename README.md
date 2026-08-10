# Universal Auto Packager

上传代码后自动识别 Python、Node.js、Electron、Tauri、Go、Rust、.NET、Java/Kotlin、C/C++、Swift、Flutter 和 Android 项目，并生成 Windows、Linux、macOS、APK/AAB 构建产物。

完整使用、上传命令和混合项目配置见 [`打包.md`](./打包.md)。

核心文件：

- `.github/workflows/auto-package.yml`：自动检测、多平台矩阵构建和 Release 发布。
- `.github/scripts/packager.py`：语言检测、默认构建器和 `packaging.json` 自定义命令执行器。

推荐通过 **Use this template** 为每个程序创建独立仓库，避免不同程序源码混在一起。
