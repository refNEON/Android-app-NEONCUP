# test_progress_callback.py
"""
回归测试：Chaquopy 传进来的 Java/Kotlin 回调对象不能被当成 Python 函数直接调用。

背景（真机上报的 bug）
--------------------
报错："PythonEngine$ProgressSink is not callable because it implements
no functional interfaces"

Chaquopy 会给**所有** Java 对象都挂上 Python 的 __call__，所以
`callable(java_object)` 恒为 True。但它只有在实现了「函数式接口」
（只有一个抽象方法的接口）时才真的能被调用，否则会在**调用那一刻**抛异常。

原来的 `_make_progress_callback` 先判断 `callable()` 就直接返回原对象，
于是把 Java 对象当函数用 —— 一跑就崩。现在改成先识别 onProgress() 方法。
"""
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mobile_api  # noqa: E402

FAILED = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{(' -> ' + str(extra)) if extra else ''}")
    if not cond:
        FAILED.append(name)


class FakeChaquopyObject:
    """
    模拟 Chaquopy 暴露给 Python 的 Java 对象：

    - 类上定义了 __call__，所以 callable(obj) 为 True（这是坑的来源）
    - 但它没有实现函数式接口，调用时抛 TypeError
    - 真正可用的是 onProgress(data) 方法
    """

    def __init__(self):
        self.received = []

    def __call__(self, *args, **kwargs):
        raise TypeError(
            "com.syncstage.app.PythonEngine$ProgressSink is not callable "
            "because it implements no functional interfaces"
        )

    def onProgress(self, data):
        self.received.append(dict(data))


class FakeJavaFunctionalInterface:
    """实现了函数式接口的对象：Python 侧可以直接 obj(data) 调用。"""

    def __init__(self):
        self.received = []

    def __call__(self, data):
        self.received.append(dict(data))


def main():
    print("TEST: Chaquopy Java 回调对象（非函数式接口）")

    sink = FakeChaquopyObject()

    # 先确认这个假对象确实复现了现场：callable() 为 True 但调用会抛异常
    check("复现现场: callable(java_obj) == True", callable(sink))
    try:
        sink({"status": "x"})
        check("复现现场: 直接调用会抛异常", False, "竟然没抛")
    except TypeError as exc:
        check("复现现场: 直接调用会抛异常", "not callable" in str(exc))

    cb = mobile_api._make_progress_callback(sink)
    check("返回的是 callable", callable(cb))
    check("不是原对象（已包一层）", cb is not sink)

    cb({"status": "aligning", "progress": 0.3, "detail": "对齐中"})
    check("onProgress 被真正触发", len(sink.received) == 1, sink.received)
    check(
        "数据内容正确",
        sink.received and sink.received[0]["status"] == "aligning",
        sink.received[0] if sink.received else None,
    )

    print()
    print("TEST: 连续多次调用")
    for i in range(3):
        cb({"status": f"s{i}", "progress": i / 3, "detail": ""})
    check("三次都收到", len(sink.received) == 4, len(sink.received))

    print()
    print("TEST: 函数式接口对象（可直接调用）")
    fi = FakeJavaFunctionalInterface()
    cb2 = mobile_api._make_progress_callback(fi)
    cb2({"status": "ok", "progress": 1.0, "detail": "done"})
    check("调用透传成功", len(fi.received) == 1, fi.received)

    print()
    print("TEST: 普通 Python 函数 / lambda")
    seen = []
    cb3 = mobile_api._make_progress_callback(lambda d: seen.append(d))
    cb3({"status": "a"})
    check("lambda 生效", len(seen) == 1)

    def fn(d):
        seen.append(d)

    cb4 = mobile_api._make_progress_callback(fn)
    cb4({"status": "b"})
    check("函数生效", len(seen) == 2)

    print()
    print("TEST: None 与无法识别的类型")
    check("None -> None", mobile_api._make_progress_callback(None) is None)

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        res = mobile_api._make_progress_callback(object())
        check("无 onProgress 也无 __call__ -> None", res is None)
        check("发出 RuntimeWarning", any(issubclass(x.category, RuntimeWarning) for x in w))

    print()
    print("TEST: 回调内部抛异常不会中断主流程")
    broken = FakeChaquopyObject()
    cb5 = mobile_api._make_progress_callback(broken)
    # 让 onProgress 崩掉
    broken.onProgress = lambda d: (_ for _ in ()).throw(RuntimeError("Java 侧炸了"))
    try:
        cb5({"status": "boom"})
        check("异常被吞掉，主流程继续", True)
    except Exception as exc:  # noqa: BLE001
        check("异常被吞掉，主流程继续", False, repr(exc))

    print()
    if FAILED:
        print(f"❌ 失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
