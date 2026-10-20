# 本项目没有开启混淆（release 的 isMinifyEnabled = false）。
# 开启混淆时必须保留下面的类，否则 Chaquopy 和 FFmpegKit 会失效。

# Chaquopy：Python 通过反射访问 Java 方法
-keep class com.chaquo.python.** { *; }
-keep class com.syncstage.app.** { *; }

# FFmpegKit
-keep class com.arthenica.ffmpegkit.** { *; }
-keepattributes *JniLibs*
-keep class com.arthenica.ffmpegkit.NativeLoader { *; }

# 保留异常信息，方便定位 Python 侧的报错
-keepattributes Signature, Exceptions, *Annotation*
