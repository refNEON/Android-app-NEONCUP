package com.syncstage.app

import android.content.Context
import android.util.Log
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

/**
 * 封装 Chaquopy，对 UI 暴露三个能力：初始化、自检、跑一次任务。
 *
 * Python 内核完全不认识 Android，Kotlin 只跟 android_entry 打交道。
 *
 * logcat 过滤：`adb logcat -s SyncStage python.stdout python.stderr`
 */
object PythonEngine {

    private const val MODULE = "android_entry"
    private const val TAG = "SyncStage"

    @Volatile
    private var started = false

    /** 必须在后台线程调用：首次启动会解压并加载 CPython，比较慢。 */
    fun ensureStarted(context: Context) {
        if (started) return

        synchronized(this) {
            if (!started) {
                val t0 = System.currentTimeMillis()
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(context.applicationContext))
                }
                started = true
                Log.i(TAG, "CPython 启动完成，耗时 ${System.currentTimeMillis() - t0} ms")
            }
        }
    }

    private fun module(context: Context): PyObject {
        ensureStarted(context)
        return Python.getInstance().getModule(MODULE)
    }

    /**
     * 初始化 Python 内核：设置数据目录 + 注入原生 FFmpeg 后端。
     * 返回 JSON 字符串，含 data_root / native_backend 等字段。
     */
    suspend fun bootstrap(context: Context): String = withContext(Dispatchers.Default) {
        ensureStarted(context)
        try {
            val result = module(context).callAttr(
                "bootstrap",
                File(context.filesDir, "syncstage").absolutePath,
                FfmpegBackend,   // 一个对象，同时提供 execute() 与 probe()
            )
            result.toString().also { Log.i(TAG, "bootstrap -> $it") }
        } catch (e: Exception) {
            Log.e(TAG, "bootstrap 抛异常", e)
            throw e
        }
    }

    /**
     * 启动 WebView 用的本地静态服务（只监听 127.0.0.1），返回端口号。
     * 网页 / 结果视频都从 http://127.0.0.1:<port>/ 加载。
     */
    suspend fun startWebServer(
        context: Context,
        webRoot: String,
        resultDir: String,
    ): Int = withContext(Dispatchers.Default) {
        ensureStarted(context)
        module(context).callAttr("start_web_server", webRoot, resultDir).toInt()
            .also { Log.i(TAG, "Web 服务已启动，端口 $it") }
    }

    suspend fun isReady(context: Context): String = withContext(Dispatchers.Default) {
        ensureStarted(context)
        module(context).callAttr("is_ready").toString()
    }

    /**
     * 跑一次任务。[onProgress] 会在 **Python 所在线程** 被调用，
     * 调用方需要自己切回主线程更新 UI。
     *
     * [manualLagMs] 是用户手填的「手动对齐延迟」，单位毫秒，字符串形式
     * （页面上就是文本框，取出来原样传）。正数延后、负数提前；
     * 空串表示不手动干预，走自动对齐。
     */
    suspend fun process(
        context: Context,
        videoPath: String,
        musicPath: String?,
        originalVolume: Double,
        alignedVolume: Double,
        manualLagMs: String = "",
        onProgress: (status: String, progress: Double, detail: String) -> Unit,
    ): String = withContext(Dispatchers.Default) {
        ensureStarted(context)

        val sink = ProgressSink(onProgress)

        val raw = module(context).callAttr(
            "start",
            videoPath,
            musicPath ?: "",
            originalVolume,
            alignedVolume,
            sink,
            manualLagMs,
        ).toString()

        // mobile_api 已经保证一定返回可序列化的 JSON。
        // 这里再兜一层，避免任何意外字符把 UI 弄崩。
        // 注意两个分支都必须是 String，否则 elvis 会推成 Any。
        runCatching { JSONObject(raw).toString() }.getOrNull() ?: JSONObject()
            .put("status", "error")
            .put("error", "Python 返回了非 JSON 内容：$raw")
            .toString()
    }

    /**
     * 波形分析：抽两条音轨 → 各压成若干峰值 → 带上延迟量返回。
     *
     * [manualLagMs] 有值就直接用（毫秒，正延后/负提前），
     * 空串则跑一次完整的自动对齐 —— 那条路慢，和正式处理的对齐阶段相当。
     *
     * 一定返回 JSON 字符串，出错也走 JSON（status=error），
     * 这样调用方不用区分异常和正常结果。
     */
    suspend fun analyzeWaveform(
        context: Context,
        videoPath: String,
        musicPath: String,
        manualLagMs: String = "",
        workDir: String,
        points: Int = 720,
    ): String = withContext(Dispatchers.Default) {
        ensureStarted(context)

        val raw = module(context).callAttr(
            "analyze_waveform",
            videoPath,
            musicPath,
            manualLagMs,
            workDir,
            points,
        ).toString()

        runCatching { JSONObject(raw).toString() }.getOrNull() ?: JSONObject()
            .put("status", "error")
            .put("error", "Python 返回了非 JSON 内容：$raw")
            .toString()
    }

    /**
     * Python 侧的 progress_callback 会调用它的 onProgress(data)。
     *
     * ⚠️ 必须实现这个「函数式接口」（只有一个抽象方法的接口）。
     * Chaquopy 会给所有 Java 对象都挂上 Python 的 __call__，
     * 但只有当类实现了函数式接口时，Python 里 `obj(...)` 才能真正调用；
     * 否则会在调用那一刻抛
     * "… is not callable because it implements no functional interfaces"。
     * Python 侧（mobile_api._make_progress_callback）已经优先走
     * onProgress() 方法式调用，这里是双保险。
     *
     * 用 PyObject 直接取值，不依赖 dict -> Map 的自动转换，
     * 避免不同 Chaquopy 版本行为差异。
     */
    fun interface PythonProgress {
        fun onProgress(data: PyObject?)
    }

    class ProgressSink(
        private val emit: (String, Double, String) -> Unit,
    ) : PythonProgress {
        override fun onProgress(data: PyObject?) {
            if (data == null) return

            val status = readField(data, "status").orEmpty()
            val detail = readField(data, "detail").orEmpty()
            val progress = readField(data, "progress")?.toDoubleOrNull() ?: 0.0

            emit(status, progress, detail)
        }

        /** dict.get(key)，缺失时 Python 返回 None，这里统一当 null 处理。 */
        private fun readField(data: PyObject, key: String): String? {
            val value = data.callAttr("get", key) ?: return null
            val text = value.toString()
            return if (text == "None" || text.isEmpty()) null else text
        }
    }
}
