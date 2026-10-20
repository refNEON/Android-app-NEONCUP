# SyncStage v0.3 项目约定

## 工作方式（用户明确要求，2026-10-20）

- **只改源码文件，不要自动打包 APK**。改完直接告知「改了哪个文件、哪几行、为什么」，把构建/安装交给用户自己决定。
- 不要每次改动都跑 `./gradlew :app:assembleDebug`，也不要 `present_files` 交付 APK。
- 除非用户明确说「打包」「构建」「出个包」，或需要验证编译错误时才构建。

## 文件职责

- `android/app/src/main/assets/web/cupnb.html` —— **App 唯一页面来源**，用户会手改。构建流程不得从 `frontend/` 覆盖它（`syncWebAssets` 已移除，仅留手动任务 `pullWebFromFrontend`）。
- `frontend/cupnb.html` —— 桌面版 Flask 用的页面，与 App 互不影响。
- `android/app/src/main/assets/web/native-bridge.js` —— Android 适配层，覆盖 `window.fetch` 转原生调用。改动须保持「分段幂等」，禁止全局提前 return。
- `backend/` —— Python 内核，构建时由 `syncPythonKernel` 复制到 `android/app/src/main/python/`。
- `backup/` —— 两份 HTML 备份（当前版 / 原始压缩包版）。

## 跨语言硬约束

- Chaquopy：Python list 无法转成 `java.util.List`。Kotlin 接收集合参数一律用 `PyObject` 再 `asList()/asMap()`。
- Chaquopy：所有 Java 对象都挂了 `__call__`，`callable()` 恒为 True。回调要用**方法名优先**（`onProgress` 等）+ 函数式接口（`fun interface`）。

## 构建环境

- 需 `env -u PYTHONPATH -u CODEBUDDY_SAFE_DELETE_ENABLED` 才能跑 gradlew（沙箱注入会拦截删除）。
- Python 固定 3.10；wheel 走 `android/wheels` 离线仓库。
