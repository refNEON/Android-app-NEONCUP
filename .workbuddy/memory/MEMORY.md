# SyncStage v0.3 项目约定

## 工作方式（用户明确要求，2026-10-20）

- **只改源码文件，不要自动打包 APK**。改完直接告知「改了哪个文件、哪几行、为什么」，把构建/安装交给用户自己决定。
- 不要每次改动都跑 `./gradlew :app:assembleDebug`，也不要 `present_files` 交付 APK。
- 除非用户明确说「打包」「构建」「出个包」，或需要验证编译错误时才构建。

## 文件职责

> **2026-10-21：桌面版已整体删除。** 工程只剩 App 一条链路。
> 已删：`backend/app.py`、`celery_app.py`、`task.py`、`start.bat`、`verify_env.py`、
> `test.py`、`requirements*.txt`、`backend/{uploads,temp,results,reference_audio}/`、
> 整个 `frontend/`、`.venv/`。**别再想着改 app.py 或 frontend/，它们不存在了。**

- `android/app/src/main/assets/web/cupnb.html` —— **全工程唯一页面文件**，用户会手改。构建不会覆盖 / 同步它。
- `android/app/src/main/assets/web/native-bridge.js` —— Android 适配层，覆盖 `window.fetch` 转原生调用。改动须保持「分段幂等」，禁止全局提前 return。
- `backend/` —— Python 内核，**只有 6 个文件**：`config.py`、`pipeline.py`、`audio_processor.py`、`video_processor.py`、`mobile_api.py`、`android_web.py`，构建时由 `syncPythonKernel` 复制到 `android/app/src/main/python/`（外加手写的 `android_entry.py`）。少一个就 ImportError。
- `backend/tests/` —— 内核端到端测试，`run_all.py` 一条命令跑 5 项（需 numpy/scipy/soundfile/soxr + 系统 ffmpeg）。
- `backup/` —— 三份 HTML 备份（当前版 / 原始压缩包版 / 桌面版删除前）+ `desktop-uncommitted-20261021/`（桌面版删除前的未提交补丁与 pip freeze）。
- **App 图标（2026-10-21 重做）**：`res/drawable/neoncup_background.xml`（深夜蓝径向渐变，自适应背景层）+ `res/mipmap-*/neoncup_foreground.webp`（透明底霓虹杯，5 密度）+ `res/mipmap-anydpi-v26/neoncup{,_round}.xml`（自适应图标）+ `res/mipmap-*/neoncup{,_round}.webp`（API<26 回落）。manifest 必须写 `@mipmap/neoncup` / `@mipmap/neoncup_round`，**别直接指 foreground**。`<adaptive-icon>` 只能放 `mipmap-anydpi-v26/`，放 `drawable/` 会 aapt2 报 "requires a sdk version of at least 26"。

## 跨语言硬约束

- Chaquopy：Python list 无法转成 `java.util.List`。Kotlin 接收集合参数一律用 `PyObject` 再 `asList()/asMap()`。
- Chaquopy：所有 Java 对象都挂了 `__call__`，`callable()` 恒为 True。回调要用**方法名优先**（`onProgress` 等）+ 函数式接口（`fun interface`）。

## 构建环境

- 需 `env -u PYTHONPATH -u CODEBUDDY_SAFE_DELETE_ENABLED` 才能跑 gradlew（沙箱注入会拦截删除）。
- Python 固定 3.10；wheel 走 `android/wheels` 离线仓库。
