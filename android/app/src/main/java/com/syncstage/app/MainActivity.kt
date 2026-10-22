package com.syncstage.app

import android.Manifest
import android.app.DownloadManager
import android.content.ContentValues
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Message
import android.provider.MediaStore
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.ConsoleMessage
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.util.concurrent.atomic.AtomicBoolean
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * 主界面 = assets/web/cupnb.html（用 WebView 加载）。
 *
 * 与网页的分工：
 *   - 网页负责所有 UI 和交互（它原本是给 Flask 写的，靠 native-bridge.js 转接）；
 *   - Kotlin 负责「选文件 → 交给 Python 内核 → 回传进度/结果」；
 *   - Python 侧只起一个 127.0.0.1 的静态服务，供页面和 <video> 使用。
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "SyncStage"
        private const val PAGE_URL = "file:///android_asset/web/cupnb.html"

        /**
         * 这些协议的链接必须留在 WebView 里，不能往外丢：
         *   file        页面自己（assets / debug_web）
         *   about/data  页面内部页面
         *   blob        页面生成的视频/音频临时地址
         *   javascript  页面里的 javascript: 伪链接
         */
        private val INTERNAL_SCHEMES = setOf("file", "about", "data", "blob", "javascript")
    }

    private lateinit var webView: WebView
    private lateinit var loadingText: TextView
    private lateinit var historyButton: Button

    /** 本地静态服务端口（只用于 /results/ 视频，0 = 还没起来）。 */
    @Volatile
    private var webPort = 0

    /** 内核 + 静态服务就绪。任务提交前会检查它。 */
    private val engineReady = CompletableDeferred<Int>()

    // ---- 网页里的 file input 走这里 ----
    private var pendingFileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingFileKind: String? = null

    /**
     * 处理 target="_blank" 链接时用的临时 WebView（探针）。
     * 它不会被挂到界面上，只负责把地址接住然后交给系统浏览器；
     * 每接一次就把上一个销毁，避免攒着泄漏。
     */
    private var externalWindowProbe: WebView? = null

    // 选完文件后在后台复制到缓存，真正的路径给 Python 用
    private var videoSource: Deferred<String?>? = null
    private var musicSource: Deferred<String?>? = null

    // taskId -> 状态，JS 侧轮询读取
    private val tasks = ConcurrentHashMap<String, TaskSnapshot>()

    // 内核同一时刻只能跑一个任务：第二个请求排队，避免两路 ffmpeg 抢内存
    private val taskMutex = Mutex()

    /**
     * 波形分析的重入保护。
     * 它也会调 FFmpeg（抽两条音轨），自动对齐还能跑几十秒，
     * 和正式任务并行会直接抢内存，所以互斥而不是排队 —— 重复点击给提示就好。
     */
    private val analyzing = AtomicBoolean(false)

    private data class TaskSnapshot(
        val state: String,
        val progress: Double,
        val detail: String,
        val outputPath: String? = null,
    )

    private val pickFile = registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        val callback = pendingFileCallback
        val kind = pendingFileKind
        pendingFileCallback = null
        pendingFileKind = null

        if (callback == null) return@registerForActivityResult
        if (uri == null) {
            callback.onReceiveValue(null)
            return@registerForActivityResult
        }

        // 先把 Uri 还给网页：它要用这个显示文件名
        callback.onReceiveValue(arrayOf(uri))

        // 页面跑在 file:// 下，WebView 不一定把 content:// URI 变成 input.files
        // （file:// 源访问 content:// 会被拒绝），那样页面会一直提示"请先上传视频"。
        // 这里额外把「选好了」这件事直接告诉页面做兜底，见 native-bridge.js。
        notifyFilePicked(if (kind == "audio") "audioFile" else "videoFile", uri)

        // 再把内容复制到缓存：Python 只认本地路径，且必须保留扩展名
        when (kind) {
            "audio" -> musicSource = lifecycleScope.async(Dispatchers.IO) {
                copyToCache(uri, "input_music")
            }
            else -> videoSource = lifecycleScope.async(Dispatchers.IO) {
                copyToCache(uri, "input_video")
            }
        }
    }

    /** 告诉页面"这个文件已经选好了"，并打印诊断日志。 */
    private fun notifyFilePicked(inputId: String, uri: Uri) {
        val name = runCatching {
            contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                val index = cursor.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                val sizeIndex = cursor.getColumnIndex(android.provider.OpenableColumns.SIZE)
                if (cursor.moveToFirst()) {
                    val display = if (index >= 0) cursor.getString(index) else null
                    val size = if (sizeIndex >= 0) cursor.getLong(sizeIndex) else 0L
                    display to size
                } else {
                    null
                }
            }
        }.getOrNull()

        val display = name?.first ?: uri.lastPathSegment ?: "$inputId"
        val size = name?.second ?: 0L
        Log.i(TAG, "已选文件 $inputId -> $display ($size B) uri=$uri")

        val call = "window.__syncStageSetFile && window.__syncStageSetFile(" +
            "${JSONObject.quote(inputId)}, ${JSONObject.quote(display)}, $size)"
        webView.evaluateJavascript(call, null)
    }

    // ---- 导出到公共「影片/SyncStage」----
    private var pendingExport: HistoryItem? = null
    private var exporting = false

    private val requestWritePermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        val item = pendingExport
        pendingExport = null
        if (granted && item != null) {
            startExport(item)
        } else {
            toast(getString(R.string.export_denied))
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        loadingText = findViewById(R.id.loadingText)
        historyButton = findViewById(R.id.historyButton)
        historyButton.setOnClickListener { showHistory() }

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true          // 页面用 localStorage 记后端地址
            mediaPlaybackRequiresUserGesture = false
            textZoom = 100                    // 系统字体放大不该破坏布局

            // 禁止整页缩放：捏合、双击放大全部失效。
            // 页面里自己的缩放（延迟波形 canvas、海报拖拽）走 PointerEvent，不受影响。
            // 注意 WebSettings 的 getter 是老命名的 supportZoom()，没有 get 前缀，
            // Kotlin 不会生成同名属性，所以这里必须显式调 setter。
            setSupportZoom(false)
            setBuiltInZoomControls(false)     // 别出现那对 +/− 浮动按钮
            setDisplayZoomControls(false)

            cacheMode = WebSettings.LOAD_NO_CACHE

            // 页面从 file:///android_asset 加载（不依赖 Python 服务，秒开）。
            // API 30+ 默认关闭文件访问，必须显式打开，否则页面读不到自己。
            allowFileAccess = true
            allowContentAccess = true

            // 视频是 http://127.0.0.1:<port>/results/…，
            // 从 file:// 页面加载属于「混合内容」，必须放行，否则 <video> 空白。
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        }
        webView.addJavascriptInterface(Bridge(), "SyncStage")
        webView.setDownloadListener { url, _, _, _, _ ->
            downloadVideo(url)
        }
        webView.webChromeClient = object : WebChromeClient() {

            /**
             * 把页面的 console 输出转发到 logcat（tag SyncStage）。
             * 页面有自己的 UI，JS 报错在界面上看不到，只能靠这里。
             * 排查"点了没反应"时先看它。
             */
            override fun onConsoleMessage(message: ConsoleMessage?): Boolean {
                val m = message ?: return false
                val text = "JS[${m.sourceId()}:${m.lineNumber()}] ${m.message()}"
                when (m.messageLevel()) {
                    ConsoleMessage.MessageLevel.ERROR -> Log.e(TAG, text)
                    ConsoleMessage.MessageLevel.WARNING -> Log.w(TAG, text)
                    else -> Log.i(TAG, text)
                }
                return true
            }

            override fun onShowFileChooser(
                view: WebView?,
                callback: ValueCallback<Array<Uri>>?,
                params: FileChooserParams?,
            ): Boolean {
                pendingFileCallback?.onReceiveValue(null)
                pendingFileCallback = callback

                val accept = params?.acceptTypes?.joinToString(" ").orEmpty()
                val isAudio = accept.contains("audio")
                pendingFileKind = if (isAudio) "audio" else "video"

                return runCatching {
                    pickFile.launch(if (isAudio) "audio/*" else "video/*")
                    true
                }.getOrDefault(false)
            }

            /**
             * 页面里 target="_blank" 或 window.open() 的链接**不会**走 shouldOverrideUrlLoading，
             * 而是走到这里。这里不开新窗口，只把地址接住后交给系统浏览器，
             * 否则 WebView 找不到新窗口，点了就是没反应。
             */
            override fun onCreateWindow(
                view: WebView?,
                isDialog: Boolean,
                isUserGesture: Boolean,
                resultMsg: Message?,
            ): Boolean {
                val transport = resultMsg?.obj as? WebView.WebViewTransport ?: return false

                externalWindowProbe?.destroy()
                val probe = WebView(this@MainActivity)
                externalWindowProbe = probe
                probe.webViewClient = object : WebViewClient() {
                    override fun shouldOverrideUrlLoading(
                        w: WebView?,
                        request: WebResourceRequest?,
                    ): Boolean {
                        // 永远返回 true：新窗口只做转发，不做任何加载
                        handleNavigation(request?.url?.toString())
                        return true
                    }
                }
                transport.webView = probe
                resultMsg.sendToTarget()
                return true
            }
        }
        webView.webViewClient = object : WebViewClient() {

            /**
             * 站外超链接一律交给系统浏览器打开，App 自己不做站内跳转。
             *
             * 只有两类留在 WebView 里：
             *   1. file:// 等页面内部协议（见 INTERNAL_SCHEMES）；
             *   2. 127.0.0.1 / localhost —— Python 内核的 /results/ 视频流走 Range 请求，
             *      一旦被外部浏览器接管，<video> 的播放和「存到影片」都会断掉。
             */
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?,
            ): Boolean {
                val req = request ?: return false
                // 子框架（iframe）不动，只管主框架的导航
                if (!req.isForMainFrame) return false
                return handleNavigation(req.url?.toString())
            }

            // API 24+ 系统只回调上面那个 WebResourceRequest 版本，
            // 这个字符串版本留给个别定制 ROM 的旧通道，逻辑完全一致。
            @Suppress("DEPRECATION", "OVERRIDE_DEPRECATION")
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                return handleNavigation(url)
            }
            override fun onPageStarted(view: WebView?, url: String?, favicon: android.graphics.Bitmap?) {
                super.onPageStarted(view, url, favicon)
                // 尽早注入：页面里的脚本在解析阶段就可能发请求。
                injectBridge()
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                injectBridge()
                // 内核若已就绪，补推一次端口（onPageStarted 时页面上下文可能还没建立）
                val port = webPort
                if (port > 0) {
                    view?.evaluateJavascript("window.__syncStagePort = $port;", null)
                }
                loadingText.visibility = View.GONE
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?,
            ) {
                if (request?.isForMainFrame == true) {
                    val detail = error?.description ?: "未知错误"
                    Log.e(TAG, "页面加载失败: ${request.url} err=$detail")
                    loadingText.visibility = View.VISIBLE
                    loadingText.text = "页面加载失败：$detail"
                }
            }
        }

        startEngineAndLoad()
    }

    // ---------------- 链接跳转策略 ----------------

    /**
     * 决定一个链接是「留在 WebView」还是「交给系统」。
     *
     * @return true  = 已接管，WebView 不要加载它
     *         false = 留在 WebView 里加载
     */
    private fun handleNavigation(url: String?): Boolean {
        if (url.isNullOrBlank()) return false

        val uri = runCatching { Uri.parse(url) }.getOrNull() ?: return false
        val scheme = uri.scheme?.lowercase() ?: return false

        // 1) 页面自身 / 页面内部机制：留在 WebView
        if (scheme in INTERNAL_SCHEMES) return false

        // 2) 本地内核服务（视频流、Range 请求）：留在 WebView，丢了就放不了视频
        val host = uri.host?.lowercase().orEmpty()
        if (host == "127.0.0.1" || host == "localhost") return false

        // 3) 其余全部交给系统：http(s) 会被浏览器接，mailto/tel/intent 由系统匹配
        return openExternally(url)
    }

    /**
     * 用系统能力打开链接。优先浏览器（http/https 只有浏览器认），
     * 没有可用应用时给个提示，避免「点了没反应」。
     */
    private fun openExternally(url: String): Boolean {
        val intent = runCatching {
            Intent(Intent.ACTION_VIEW, Uri.parse(url)).apply {
                addCategory(Intent.CATEGORY_BROWSABLE)
                // 从 App 内部拨给外部应用，必须带 NEW_TASK
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
        }.getOrNull()

        if (intent == null || intent.resolveActivity(packageManager) == null) {
            Log.w(TAG, "没有 App 能处理该链接: $url")
            toast("没有可用的浏览器打开这个链接")
            return true   // 已接管，别让 WebView 再加载一遍再报错
        }

        return runCatching {
            startActivity(intent)
            Log.i(TAG, "已交给系统打开: $url")
            true
        }.getOrElse {
            Log.e(TAG, "打开外部链接失败: $url", it)
            toast("打开链接失败")
            true
        }
    }

    // ---------------- 启动内核 + 加载页面 ----------------

    /**
     * 页面和内核**解耦**启动。
     *
     * 以前是先起 Python 的 http 服务、再用 http://127.0.0.1:port/cupnb.html 打开页面，
     * 结果内核启动慢或服务起不来时，界面就永远停在「正在加载」。
     * 现在页面直接从 assets 读（file://），几十毫秒就能显示；
     * Python 只在后台起服务，专门给 <video> 提供 /results/ 的 Range 请求。
     */
    private fun startEngineAndLoad() {
        loadingText.visibility = View.VISIBLE
        loadingText.text = getString(R.string.loading_page)

        val url = resolvePageUrl()
        val t0 = System.currentTimeMillis()
        webView.loadUrl(url)
        Log.i(TAG, "开始加载页面 $url")
        if (url != PAGE_URL) toast("正在使用调试页面（debug_web/cupnb.html）")

        lifecycleScope.launch {
            val port = runCatching {
                withContext(Dispatchers.IO) {
                    PythonEngine.ensureStarted(this@MainActivity)
                    PythonEngine.bootstrap(this@MainActivity)

                    val webRoot = prepareWebRoot()
                    val results = File(filesDir, "syncstage/results").apply { mkdirs() }
                    PythonEngine.startWebServer(
                        this@MainActivity,
                        webRoot.absolutePath,
                        results.absolutePath,
                    )
                }
            }.getOrElse { error ->
                Log.e(TAG, "内核启动失败", error)
                engineReady.completeExceptionally(error)
                runOnUiThread {
                    toast("处理内核启动失败：${error.message}")
                }
                return@launch
            }

            webPort = port
            engineReady.complete(port)
            Log.i(TAG, "内核就绪，端口 $port，总耗时 ${System.currentTimeMillis() - t0} ms")

            // 页面可能已经加载完，也可能还没；两种情况都通知一次。
            webView.evaluateJavascript("window.__syncStagePort = $port;", null)
        }
    }

    /**
     * 决定加载哪个页面，优先级从高到低：
     *
     * 1. `<外部files>/debug_web/cupnb.html`
     *    即 `/sdcard/Android/data/com.syncstage.app/files/debug_web/cupnb.html`。
     *    调试用：把改好的 HTML 推到这个路径，重启 App 就能看到效果，
     *    **不用重新构建 APK**。不需要任何存储权限，`adb push` 直接可写。
     *    删掉这个文件就回到 assets 里的正式版本。
     * 2. `file:///android_asset/web/cupnb.html`
     *    正式来源，就是 App 工程里的那个文件，想改界面直接改它。
     */
    private fun resolvePageUrl(): String {
        val override = getExternalFilesDir(null)?.let { File(it, "debug_web/cupnb.html") }
        if (override != null && override.isFile) return "file://${override.absolutePath}"
        return PAGE_URL
    }

    /** 把 assets/web 整个搬到 filesDir，Python 的 http 服务从磁盘读。 */
    private fun prepareWebRoot(): File {
        val root = File(filesDir, "syncstage/web")
        root.mkdirs()
        copyAsset("web", root)
        return root
    }

    private fun copyAsset(assetPath: String, target: File) {
        val children = assets.list(assetPath)
        if (children.isNullOrEmpty()) {
            assets.open(assetPath).use { input ->
                target.parentFile?.mkdirs()
                target.outputStream().use { output -> input.copyTo(output) }
            }
            return
        }
        target.mkdirs()
        children.forEach { name -> copyAsset("$assetPath/$name", File(target, name)) }
    }

    private fun injectBridge() {
        runCatching {
            val script = assets.open("web/native-bridge.js")
                .bufferedReader()
                .use { it.readText() }
            webView.evaluateJavascript(script, null)
        }
    }

    // ---------------- JS bridge ----------------

    inner class Bridge {

        /** 页面想主动往 logcat 写东西时用（adb logcat -s SyncStage）。 */
        @JavascriptInterface
        fun log(message: String) {
            Log.i(TAG, "JS: $message")
        }

        /** 网页提交任务。返回 taskId，进度用 taskStatus 轮询。 */
        @JavascriptInterface
        fun startTask(payload: String): String {
            val taskId = UUID.randomUUID().toString().replace("-", "").take(12)
            Log.i(TAG, "startTask -> $taskId payload=$payload")
            tasks[taskId] = TaskSnapshot("PENDING", 0.0, "已提交，准备处理")
            lifecycleScope.launch { runTask(taskId, payload) }
            return taskId
        }

        @JavascriptInterface
        fun taskStatus(taskId: String): String {
            val snapshot = tasks[taskId]
                ?: return JSONObject()
                    .put("state", "FAILURE")
                    .put("detail", "任务不存在")
                    .toString()

            val json = JSONObject()
                .put("state", snapshot.state)
                .put("progress", snapshot.progress)
                .put("detail", snapshot.detail)

            val output = snapshot.outputPath
            if (!output.isNullOrBlank()) {
                val folder = File(output).parentFile?.name
                val port = webPort
                if (folder != null && port > 0) {
                    // 必须是绝对地址：页面跑在 file:// 下，
                    // 相对路径 /results/... 会被解析成 file:///results/... 而打不开。
                    json.put("output_url", "http://127.0.0.1:$port/results/$folder/output.mp4")
                }
            }
            return json.toString()
        }

        /**
         * 请求一次「延迟波形分析」。
         *
         * payload: {"manualDelay": "..."} —— 和提交任务共用同一个字段名，
         * 页面里不用维护两套参数名。
         *
         * 采用「立即返回 + 完成后回调」的方式，不做轮询：
         * 自动对齐要几十秒，JS 侧用一个 Promise 等 window.__syncStageWaveform。
         */
        @JavascriptInterface
        fun analyzeWaveform(payload: String) {
            val options = runCatching { JSONObject(payload) }.getOrDefault(JSONObject())
            val manualLagMs = options.optString("manualDelay").trim()

            lifecycleScope.launch {
                val json = runWaveformAnalysis(manualLagMs)
                // 回调必须回主线程；结果里可能有引号/换行，必须 quote 转义，
                // 否则拼出来的 JS 会语法错误，页面那边就是"点了没反应"
                webView.post {
                    webView.evaluateJavascript(
                        "window.__syncStageWaveform && window.__syncStageWaveform(" +
                            JSONObject.quote(json) + ");",
                        null,
                    )
                }
            }
        }

        private suspend fun runWaveformAnalysis(manualLagMs: String): String {
            if (!analyzing.compareAndSet(false, true)) {
                return errorJson("正在分析中，请等上一次分析结束")
            }
            return try {
                if (!engineReady.isCompleted) {
                    return errorJson("处理内核还在启动，请等几秒后重试")
                }
                engineReady.await()

                // 文件可能还在复制，await() 会等它完成
                val video = videoSource?.await()
                    ?: return errorJson("请先选择手元视频")
                val music = musicSource?.await()
                    ?: return errorJson("请先选择参考音乐")
                if (video.isBlank()) return errorJson("视频读取失败，请重新选择")
                if (music.isBlank()) return errorJson("音乐读取失败，请重新选择")

                PythonEngine.analyzeWaveform(
                    context = this@MainActivity,
                    videoPath = video,
                    musicPath = music,
                    manualLagMs = manualLagMs,
                    workDir = File(cacheDir, "analysis").absolutePath,
                )
            } catch (e: Exception) {
                Log.e(TAG, "波形分析失败", e)
                errorJson(e.message ?: "分析失败")
            } finally {
                analyzing.set(false)
            }
        }

        private fun errorJson(message: String): String = JSONObject()
            .put("status", "error")
            .put("error", message)
            .toString()
    }

    private fun updateTask(taskId: String, state: String, progress: Double, detail: String) {
        val current = tasks[taskId]
        // 已经结束的任务不再被迟到的进度回调改回 RUNNING
        if (current?.state == "SUCCESS" || current?.state == "FAILURE") return
        tasks[taskId] = TaskSnapshot(
            state = state,
            progress = progress.coerceIn(0.0, 1.0),
            detail = detail,
            outputPath = current?.outputPath,
        )
    }

    private suspend fun runTask(taskId: String, payload: String) = taskMutex.withLock {
        try {
            // 页面可能比内核先就绪（页面是本地打开，秒出）。
            // 内核没起来时直接给出可读的提示，而不是抛个莫名其妙的异常。
            if (!engineReady.isCompleted) {
                tasks[taskId] =
                    TaskSnapshot("FAILURE", 0.0, "处理内核还在启动，请等几秒后重新点击开始")
                return@withLock
            }
            // 内核启动失败时 await() 会直接抛出当初的原因
            engineReady.await()

            val options = runCatching { JSONObject(payload) }.getOrDefault(JSONObject())
            updateTask(taskId, "RUNNING", 0.01, "正在准备素材…")

            // 文件可能还在复制；await 会等它完成
            val video = videoSource?.await()
                ?: throw IllegalStateException("请先选择手元视频")
            val music = musicSource?.await()
                ?: throw IllegalStateException("请先选择参考音乐")
            if (video.isBlank()) throw IllegalStateException("视频读取失败，请重新选择")
            if (music.isBlank()) throw IllegalStateException("音乐读取失败，请重新选择")

            val originalVolume = options.optString("originalVolume").toDoubleOrNull() ?: 0.4
            val alignedVolume = options.optString("alignedVolume").toDoubleOrNull() ?: 0.8

            // 手填的对齐延迟（毫秒）。空串 = 自动对齐。
            // 数值合法性交给 Python 侧统一判定（那边有范围和具体报错），
            // 这里只负责原样透传，避免两套规则不一致。
            val manualLagMs = options.optString("manualDelay").trim()

            updateTask(taskId, "RUNNING", 0.02, "开始处理…")
            val raw = PythonEngine.process(
                context = this@MainActivity,
                videoPath = video,
                musicPath = music,
                originalVolume = originalVolume,
                alignedVolume = alignedVolume,
                manualLagMs = manualLagMs,
            ) { _, progress, detail ->
                updateTask(taskId, "RUNNING", progress, detail.ifBlank { "处理中" })
            }

            val result = JSONObject(raw)
            if (result.optString("status") == "success") {
                val output = result.optString("output_path")
                saveResultMeta(result)
                tasks[taskId] = TaskSnapshot("SUCCESS", 1.0, "处理完成", output)
            } else {
                tasks[taskId] = TaskSnapshot(
                    "FAILURE", 0.0, result.optString("error", "处理失败")
                )
            }
        } catch (e: Exception) {
            tasks[taskId] = TaskSnapshot("FAILURE", 0.0, e.message ?: "未知错误")
        }
    }

    // ---------------- 文件 ----------------

    /**
     * SAF 给的是 content:// URI，Python 内核只接受本地文件路径，
     * 所以先复制到 App 私有缓存目录。
     */
    private suspend fun copyToCache(uri: Uri, baseName: String): String? =
        withContext(Dispatchers.IO) {
            val dir = File(cacheDir, "inputs").apply { mkdirs() }

            // 必须保留原扩展名：内核按扩展名校验视频/音乐格式。
            val target = File(dir, baseName + guessExtension(uri))

            runCatching {
                contentResolver.openInputStream(uri)?.use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
                target.absolutePath
            }.getOrElse {
                null
            }
        }

    private fun guessExtension(uri: Uri): String {
        // 1) 问 MediaStore 拿真实文件名，这是最准的
        val displayName = runCatching {
            contentResolver.query(uri, arrayOf(android.provider.OpenableColumns.DISPLAY_NAME), null, null, null)
                ?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
        }.getOrNull().orEmpty()

        val fromName = displayName.substringAfterLast('.', "").lowercase()
        if (fromName.isNotEmpty()) return ".$fromName"

        // 2) 退一步用 MIME
        val mime = contentResolver.getType(uri).orEmpty()
        val fromMime = android.webkit.MimeTypeMap.getSingleton().getExtensionFromMimeType(mime)
        if (!fromMime.isNullOrEmpty()) return ".$fromMime"

        // 3) 兜底：音频别再兜成 .mp4
        return if (mime.startsWith("audio/")) ".mp3" else ".mp4"
    }

    // ---------------- 历史结果 ----------------

    /** 内核的 RESULT_DIR：<data_root>/results/<task_id>/output.mp4 */
    private val resultsRoot: File
        get() = File(filesDir, "syncstage/results")

    private data class HistoryItem(val dir: File, val file: File, val meta: JSONObject?)

    /**
     * 把本次结果的关键信息写到结果目录里，供历史列表回显
     * （内核本身不落盘元数据，这里补一份，失败也不影响主流程）。
     */
    private fun saveResultMeta(result: JSONObject) {
        runCatching {
            val outputPath = result.optString("output_path")
            if (outputPath.isBlank()) return@runCatching
            val dir = File(outputPath).parentFile ?: return@runCatching
            File(dir, "meta.json").writeText(result.toString(2))
        }
    }

    private fun scanHistory(): List<HistoryItem> {
        val dirs = resultsRoot.listFiles { f -> f.isDirectory } ?: return emptyList()
        return dirs.mapNotNull { dir ->
            val output = File(dir, "output.mp4")
            if (!output.isFile || output.length() == 0L) return@mapNotNull null
            HistoryItem(dir, output, readMeta(dir))
        }.sortedByDescending { it.file.lastModified() }
    }

    private fun readMeta(dir: File): JSONObject? = runCatching {
        val file = File(dir, "meta.json")
        if (file.isFile) JSONObject(file.readText()) else null
    }.getOrNull()

    /**
     * 历史记录面板。
     *
     * 结果在 App 私有目录，系统文件管理器没有权限访问，所以用应用内列表：
     * 点条目直接播放（对比最佳版本），右侧可导出到相册或删除释放空间。
     */
    private fun showHistory() {
        val items = scanHistory()

        if (items.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.history)
                .setMessage(R.string.history_empty)
                .setPositiveButton(android.R.string.ok, null)
                .show()
            return
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), dp(8))
        }
        items.forEach { container.addView(buildHistoryRow(it)) }
        container.addView(TextView(this).apply {
            text = getString(R.string.history_hint)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            alpha = 0.7f
            setPadding(0, dp(14), 0, 0)
        })

        val total = items.sumOf { it.file.length() }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.history_title, items.size, formatSize(total)))
            .setView(ScrollView(this).apply { addView(container) })
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun buildHistoryRow(item: HistoryItem): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, dp(10))
        }

        val texts = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        texts.addView(TextView(this).apply {
            text = formatTime(item.file.lastModified())
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
        })
        texts.addView(TextView(this).apply {
            text = historySubtitle(item)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            alpha = 0.7f
        })
        row.addView(
            texts,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
        )

        // 导出到公共目录（文件管理器/相册可见），再决定删不删私有目录那份
        row.addView(compactButton(R.string.export) { exportToMovies(item) })
        row.addView(compactButton(R.string.delete) { confirmDelete(item) })

        // 点条目 = 播放这一版，方便逐个试听挑最佳版本。
        row.setOnClickListener { playFile(item.file.absolutePath) }
        return row
    }

    private fun historySubtitle(item: HistoryItem): String {
        val parts = mutableListOf(formatSize(item.file.length()))
        val meta = item.meta
        if (meta != null) {
            val alignment = meta.optJSONObject("audio_alignment")
            alignment?.optString("confidence")?.takeIf { it.isNotBlank() }
                ?.let { parts += "可信度 $it" }
            val lag = alignment?.optDouble("lag_seconds", Double.NaN) ?: Double.NaN
            if (!lag.isNaN()) parts += String.format(Locale.US, "偏移 %.3fs", lag)
            meta.optString("song_name").takeIf { it.isNotBlank() }?.let { parts += it }
        }
        return parts.joinToString(" · ")
    }

    private fun compactButton(textRes: Int, onClick: () -> Unit): Button =
        Button(this, null, android.R.attr.borderlessButtonStyle).apply {
            text = getString(textRes)
            minWidth = 0
            minimumWidth = 0
            setPadding(dp(8), dp(4), dp(8), dp(4))
            setOnClickListener { onClick() }
        }

    /** 播放任意一个结果文件（当前结果和历史版本共用）。 */
    private fun playFile(path: String) {
        val file = File(path)
        if (!file.isFile) {
            toast("文件已经不存在了")
            return
        }
        val uri = FileProvider.getUriForFile(this, "${packageName}.fileprovider", file)
        val intent = Intent(Intent.ACTION_VIEW, uri).apply {
            setDataAndType(uri, "video/mp4")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        runCatching { startActivity(intent) }
            .onFailure { toast("没有可以播放视频的应用") }
    }

    /**
     * 页面里的「下载视频」是 <a href="http://127.0.0.1:port/results/<id>/output.mp4">，
     * WebView 自己不会处理下载。这里把 URL 还原成本地文件，直接复用导出逻辑，
     * 保存到「影片/SyncStage」——和顶栏历史面板里的「导出」完全一致。
     */
    private fun downloadVideo(url: String) {
        val folder = Regex("/results/([^/]+)/").find(url)?.groupValues?.getOrNull(1)
        val dir = folder?.let { File(resultsRoot, it) }
        val file = dir?.let { File(it, "output.mp4") }
        if (dir != null && file != null && file.isFile) {
            exportToMovies(HistoryItem(dir, file, readMeta(dir)))
            return
        }

        // 万一不是本地结果文件，退回系统下载器
        runCatching {
            val request = DownloadManager.Request(Uri.parse(url))
                .setTitle("SyncStage 结果视频")
                .setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED
                )
            (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
            toast("已加入系统下载队列")
        }.onFailure { toast("下载失败：${it.message}") }
    }

    /**
     * 导出到公共目录 Movies/SyncStage。
     *
     * 私有目录里的结果文件只有本 App 能访问，导出后文件管理器/相册才看得到，
     * 也方便用户自己再拷贝或备份。
     */
    private fun exportToMovies(item: HistoryItem) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q &&
            checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            pendingExport = item
            requestWritePermission.launch(Manifest.permission.WRITE_EXTERNAL_STORAGE)
            return
        }
        startExport(item)
    }

    private fun startExport(item: HistoryItem) {
        if (exporting) {
            toast("上一个导出还没完成，请稍候")
            return
        }
        exporting = true
        toast(getString(R.string.export_doing))

        lifecycleScope.launch {
            val result = withContext(Dispatchers.IO) { runCatching { exportFile(item.file) } }
            exporting = false
            result.onSuccess {
                toast(getString(R.string.export_done))
            }.onFailure {
                toast(getString(R.string.export_failed, it.message ?: "未知错误"))
            }
        }
    }

    /** 返回导出后的 URI；失败抛异常，由调用方统一提示。 */
    private fun exportFile(file: File): Uri {
        val stamp = SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US)
            .format(Date(file.lastModified()))
        val name = "syncstage_$stamp.mp4"

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val resolver = contentResolver
            val values = ContentValues().apply {
                put(MediaStore.Video.Media.DISPLAY_NAME, name)
                put(MediaStore.Video.Media.MIME_TYPE, "video/mp4")
                put(
                    MediaStore.Video.Media.RELATIVE_PATH,
                    "${Environment.DIRECTORY_MOVIES}/SyncStage",
                )
                // 复制完成前对其他应用不可见，避免相册里出现半截文件
                put(MediaStore.Video.Media.IS_PENDING, 1)
            }
            val uri = resolver.insert(MediaStore.Video.Media.EXTERNAL_CONTENT_URI, values)
                ?: throw IOException("媒体库拒绝创建条目")

            try {
                resolver.openOutputStream(uri)?.use { output ->
                    file.inputStream().use { input -> input.copyTo(output) }
                } ?: throw IOException("无法写入媒体库")

                values.clear()
                values.put(MediaStore.Video.Media.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
                return uri
            } catch (e: Exception) {
                runCatching { resolver.delete(uri, null, null) }
                throw e
            }
        }

        // Android 9 及以下：直接写公共目录 + 通知媒体扫描
        @Suppress("DEPRECATION")
        val dir = File(
            Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MOVIES),
            "SyncStage",
        ).apply { mkdirs() }
        if (!dir.isDirectory) throw IOException("无法创建 Movies/SyncStage 目录")

        val target = File(dir, name)
        file.inputStream().use { input ->
            target.outputStream().use { output -> input.copyTo(output) }
        }
        MediaScannerConnection.scanFile(
            this,
            arrayOf(target.absolutePath),
            arrayOf("video/mp4"),
            null,
        )
        return Uri.fromFile(target)
    }

    private fun confirmDelete(item: HistoryItem) {
        AlertDialog.Builder(this)
            .setTitle(R.string.delete_title)
            .setMessage(
                getString(
                    R.string.delete_message,
                    formatTime(item.file.lastModified()),
                    formatSize(item.file.length()),
                )
            )
            .setPositiveButton(R.string.delete) { _, _ ->
                val removed = runCatching { item.dir.deleteRecursively() }.getOrDefault(false)
                toast(if (removed) "已删除该版本" else "删除失败，请重试")
                showHistory()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    private fun formatSize(bytes: Long): String = when {
        bytes >= (1L shl 30) -> String.format(
            Locale.US, "%.2f GB", bytes / 1024.0 / 1024.0 / 1024.0
        )
        bytes >= (1L shl 20) -> String.format(Locale.US, "%.1f MB", bytes / 1024.0 / 1024.0)
        bytes >= (1L shl 10) -> String.format(Locale.US, "%.0f KB", bytes / 1024.0)
        else -> "$bytes B"
    }

    private fun formatTime(millis: Long): String =
        SimpleDateFormat("MM-dd HH:mm", Locale.getDefault()).format(Date(millis))

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun toast(text: String) {
        runOnUiThread { Toast.makeText(this, text, Toast.LENGTH_LONG).show() }
    }
}
