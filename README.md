# 方块启动器

一个面向 Windows 和 macOS 的 Minecraft Java 版启动器项目。

## 当前进度

启动器提供本机 Python 后端与网页界面。下载中心通过本机服务读取 Mojang 官方版本清单，完整安装所选原版版本的客户端、当前操作系统依赖、原生库、资源索引和资源文件；下载可取消并显示进度，每个文件在安装前校验官方 SHA-1 与大小，临时网络错误会自动重试，已验证的文件可在重试安装时复用。官方清单会缓存在本机，网络暂时不可用时仍可浏览上次成功读取的版本和元数据。游戏文件保存在本机 `~/.mc-pcl/game` 目录。

已加入 Java 版本检查、按所选 Minecraft 版本下载 Mojang 官方 Java 运行时、启动参数与内存设置、Microsoft 设备码登录，以及使用正版账号启动已安装的原版游戏。Java 运行时保存在 `~/.mc-pcl/java`，下载后会逐文件校验 SHA-1。登录需要你自己的 Microsoft OAuth 公共客户端 ID，账号还必须拥有 Minecraft Java 版授权。启动器不会提供离线/盗版账号绕过。

## 桌面打包

桌面包会启动本机服务并打开默认浏览器；网页资源已打进程序。打包产物不包含 Minecraft 文件或 Java，首次使用时仍可在启动器中下载。

在 macOS 或 Windows 上安装 Python 3.12+ 后，可在项目目录运行：

```sh
python -m pip install -r requirements-build.txt
python -m PyInstaller --noconfirm --clean MCPCL.spec
```

macOS 生成 `dist/方块启动器.app`，Windows 生成 `dist/MCPCL.exe`。Windows `.exe` 必须在 Windows 上构建；macOS `.app` 必须在 macOS 上构建。也可在 GitHub Actions 的 **Build desktop packages** 工作流中手动启动构建，或推送 `v` 开头的标签；工作流会分别生成 Windows x64 和 macOS 包，供下载 workflow artifact。未签名/未公证的应用可能会触发系统安全提示。

打包后启动日志保存在 `~/.mc-pcl/logs/launcher.log`。

内存可设置为 2–16 GB；请为系统和其他程序预留内存。Java 参数请每行填写一个、不含空格且以 `-` 开头的选项。

## 运行启动器

需要 Python 3.10 或更高版本。可以先不安装 Java；在下载中心选择 Minecraft 版本后，可下载对应的 Mojang Java 运行时。启动本机服务：

```sh
python3 launcher.py
```

启动器会尝试打开浏览器；如果没有自动打开，请使用终端显示的 `http://127.0.0.1:<端口>/` 地址。关闭终端或按 `Ctrl+C` 会停止服务。只绑定本机回环地址，不向局域网开放。

## 配置 Microsoft 登录

1. 在 [Microsoft Entra 管理中心](https://entra.microsoft.com/)注册一个应用，并选择支持个人 Microsoft 账号的应用类型。
2. 在身份验证设置中启用公共客户端/移动和桌面应用及设备代码流；不需要也不要创建或填写 Client Secret。
3. 将应用的 Application (client) ID 填入启动器“启动设置”中的 Microsoft OAuth 客户端 ID 并保存。
4. 点击“登录”，在 Microsoft 官方网页输入设备代码并完成授权。

也可以在启动器进程启动前设置 `MC_PCL_CLIENT_ID` 环境变量。账户的密码不会经过启动器；本机保存 Microsoft 刷新令牌和 Minecraft 会话令牌，退出登录会删除本机令牌。

## 后续步骤

1. 为 Windows `.exe` 和 macOS `.app` 构建并验证正式桌面安装包。
2. 增加多个游戏实例、Mod 加载器和大型整合包支持。
