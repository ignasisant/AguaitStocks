// Telegram webhook receiver for TopStocks.
//
// Telegram POSTs each bot update here. The Worker does exactly two things:
// store the update as one JSON object in the R2 bucket (the same bucket the
// app mirrors user data to, under data/tg_updates/), and fire a GitHub
// repository_dispatch so the Actions job (`stocks telegram-chat`) wakes up,
// drains the queue and answers. No bot token, storage keys or LLM keys live
// here — only the webhook secret and a fine-grained GitHub PAT.
//
// If the R2 put fails we return 500 so Telegram retries; the zero-padded
// update_id key makes the retry an idempotent rewrite. If only the dispatch
// fails the update is already safe in R2 and the next message's dispatch
// drains the backlog.

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("ok");
    const secret = request.headers.get("x-telegram-bot-api-secret-token");
    if (secret !== env.WEBHOOK_SECRET)
      return new Response("forbidden", { status: 403 });

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("ok"); // not JSON — nothing to queue
    }
    if (!update || typeof update.update_id !== "number")
      return new Response("ok");

    const key =
      "data/tg_updates/" + String(update.update_id).padStart(12, "0") + ".json";
    await env.BUCKET.put(key, JSON.stringify(update));

    try {
      await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
        method: "POST",
        headers: {
          authorization: `Bearer ${env.GITHUB_PAT}`,
          accept: "application/vnd.github+json",
          "content-type": "application/json",
          "user-agent": "aguait-telegram-webhook",
        },
        body: JSON.stringify({ event_type: "telegram-update" }),
      });
    } catch {
      // Update is safe in R2; the next dispatch drains the backlog.
    }
    return new Response("ok");
  },
};
