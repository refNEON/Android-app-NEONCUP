package com.syncstage.app

import com.arthenica.ffmpegkit.FFmpegKit
import com.arthenica.ffmpegkit.FFprobeKit
import com.arthenica.ffmpegkit.ReturnCode
import com.chaquo.python.PyObject

/**
 * 原生 FFmpeg / FFprobe 后端。
 *
 * 这两个方法会被 android_entry.bootstrap(...) 包成 Python 回调，
 * 最终由 video_processor.MediaBackend 调用。约定：
 *   - execute(args, timeout)：args 不含程序名；失败要抛异常
 *   - probe(path, timeout)  ：返回 ffprobe -of json 的**原始字符串**
 */
object FfmpegBackend {

    /** FFmpeg 单条命令最长允许跑多久（秒）。 */
    private const val EXECUTE_TIMEOUT_SECONDS = 1800L
    private const val PROBE_TIMEOUT_SECONDS = 30L

    /**
     * 由 Python 内核调用：android_entry.execute_fn(args, timeout)。
     *
     * ⚠️ 参数**必须**声明成 PyObject，不能写 List<Any?> / List<String>。
     * Chaquopy 不做 Python list → java.util.List 的隐式转换，写 List 会在
     * 运行时抛 "cannot convert list object to java.util.List"。
     * 用 PyObject 接住后自己 asList() 取元素，跟 Chaquopy 版本无关。
     */
    @JvmStatic
    fun execute(args: PyObject, timeout: Double) {
        val arguments = args.asList().map { it.toString() }.toTypedArray()

        val session = FFmpegKit.executeWithArguments(arguments)

        if (!ReturnCode.isSuccess(session.returnCode)) {
            val logs = session.allLogsAsString?.takeLast(4000).orEmpty()
            throw RuntimeException(
                "FFmpeg 执行失败，退出码 ${session.returnCode}\n$logs"
            )
        }
    }

    @JvmStatic
    fun probe(path: String, timeout: Double): String {
        val arguments = arrayOf(
            "-v", "error",
            "-show_format",
            "-show_streams",
            "-of", "json",
            path,
        )

        val session = FFprobeKit.executeWithArguments(arguments)

        if (!ReturnCode.isSuccess(session.returnCode)) {
            val logs = session.allLogsAsString?.takeLast(4000).orEmpty()
            throw RuntimeException(
                "FFprobe 执行失败，退出码 ${session.returnCode}\n$logs"
            )
        }

        val output = session.output
        if (output.isNullOrBlank()) {
            throw RuntimeException("FFprobe 没有返回任何内容: $path")
        }

        return output
    }
}
