/*
 * native-bridge.js
 *
 * 在 onPageFinished 时注入，作用是让 cupnb.html **一行都不用改**
 * 就能在 App 里工作：
 *
 *   POST /api/upload      -> 交给原生 SyncStage.startTask()，
 *                            返回一个假的 JSON 响应（含 task_id）。
 *                            文件本身不经过网络：用户点 file input 时，
 *                            Kotlin 已经把内容复制到缓存并记住了路径。
 *   GET  /api/tasks/<id>  -> 交给原生 SyncStage.taskStatus()，
 *                            返回 {state, progress, detail, output_url}。
 *   其他请求               -> 原样走真正的 fetch（本地 http 服务）。
 *
 * 结果视频的 output_url 是 /results/<task_id>/output.mp4，
 * 由 Python 侧的小 http 服务提供（支持 Range，可拖动进度条）。
 */
(function () {
  if (!window.SyncStage || window.__syncStageBridgeReady) return;
  window.__syncStageBridgeReady = true;

  var realFetch = window.fetch ? window.fetch.bind(window) : null;

  function jsonResponse(payload, status) {
    return new Response(JSON.stringify(payload), {
      status: status || 200,
      headers: { "Content-Type": "application/json" },
    });
  }

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
})();
