/*
 * native-bridge.js
 *
 * 让 cupnb.html **一行都不用改** 就能在 App 里工作：
 *
 *   POST /api/upload      -> 交给原生 SyncStage.startTask()，
 *                            返回一个假的 JSON 响应（含 task_id）。
 *                            文件本身不经过网络：用户点 file input 时，
 *                            Kotlin 已经把内容复制到缓存并记住了路径。
 *   GET  /api/tasks/<id>  -> 交给原生 SyncStage.taskStatus()，
 *                            返回 {state, progress, detail, output_url}。
 *   其他请求               -> 原样走真正的 fetch（本地 http 服务）。
 *
 * ⚠️ 注入时机：onPageStarted 和 onPageFinished 各注入一次。
 *    onPageStarted 时 DOM 还没解析完，所以这里**不能整体早退**，
 *    否则第二次注入会被挡住、文件兜底永远装不上。
 *    下面两段各自用独立标记做幂等。
 */
(function () {
  if (!window.SyncStage) return;

  /* =========================================================
     一、接管 fetch（只装一次）
     ========================================================= */
  if (!window.__syncStageFetchReady) {
    window.__syncStageFetchReady = true;

    var realFetch = window.fetch ? window.fetch.bind(window) : null;

    var jsonResponse = function (payload, status) {
      return new Response(JSON.stringify(payload), {
        status: status || 200,
        headers: { "Content-Type": "application/json" },
      });
    };

    window.fetch = function (input, init) {
      var url = typeof input === "string" ? input : (input && input.url) || "";

      if (url.indexOf("/api/upload") >= 0) {
        var payload = {};
        var body = init && init.body;
        if (body && typeof body.forEach === "function") {
          // FormData -> 普通对象；File 字段跳过（原生已经拿到文件了）
          body.forEach(function (value, key) {
            if (typeof value === "string") payload[key] = value;
          });
        }
        try {
          var taskId = window.SyncStage.startTask(JSON.stringify(payload));
          return Promise.resolve(jsonResponse({ task_id: taskId }, 200));
        } catch (err) {
          return Promise.resolve(jsonResponse({ error: String(err) }, 500));
        }
      }

      var match = url.match(/\/api\/tasks\/([^\/?#]+)/);
      if (match) {
        try {
          return Promise.resolve(
            jsonResponse(JSON.parse(window.SyncStage.taskStatus(match[1])), 200)
          );
        } catch (err) {
          return Promise.resolve(jsonResponse({ detail: String(err) }, 500));
        }
      }

      return realFetch
        ? realFetch(input, init)
        : Promise.reject(new Error("fetch unavailable"));
    };
  }

  /* =========================================================
     二、file input 兜底（每次注入都尝试，元素出现后再装）

     页面是 file:// 加载的，WebView 不一定会把系统文件选择器返回的
     content:// URI 变成 input.files（file:// 源访问 content:// 会被拒），
     那样页面会一直提示"请先上传视频文件"，点提交毫无反应。

     Kotlin 选完文件后调用 __syncStageSetFile(id, name, size)，
     这里用一个假 File 顶上。原生 getter 有值时优先用原生的。
     ========================================================= */
  var nativeFilesGetter = null;
  try {
    var desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "files");
    if (desc && desc.get) nativeFilesGetter = desc.get;
  } catch (e) {
    nativeFilesGetter = null;
  }

  function patchFileInput(id) {
    var input = document.getElementById(id);
    if (!input || input.__syncStagePatched) return false;

    try {
      Object.defineProperty(input, "files", {
        configurable: true,
        get: function () {
          if (nativeFilesGetter) {
            try {
              var real = nativeFilesGetter.call(this);
              if (real && real.length) return real;
            } catch (e) {
              /* 原生 getter 失败时走下面的兜底 */
            }
          }
          return this.__syncStageFile ? [this.__syncStageFile] : [];
        },
      });
      input.__syncStagePatched = true;
      return true;
    } catch (e) {
      return false;
    }
  }

  function patchAll() {
    patchFileInput("videoFile");
    patchFileInput("audioFile");
  }

  patchAll();

  window.__syncStageSetFile = function (id, name, size) {
    patchFileInput(id);
    var input = document.getElementById(id);
    if (!input) return "no input: " + id;

    input.__syncStageFile = { name: name, size: size, __syncStage: true };
    // 触发页面自己的 change 处理（显示文件名、高亮 dropzone 等）
    try {
      input.dispatchEvent(new Event("change"));
    } catch (e) {
      /* 老 WebView 没有 Event 构造器，忽略即可 */
    }
    return "ok";
  };

  // DOM 就绪后再补一次（onPageStarted 注入时元素还不存在）
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", patchAll);
  }
})();
