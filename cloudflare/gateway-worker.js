// estama-r2 gateway Worker.
// Maps the URL path to an R2 key in the bucket bound as DB.
// Auth: every request must carry "Authorization: Bearer <GATEWAY_TOKEN>"
// (a Worker secret). Fails closed when the secret is not set.
//   PUT    /<key>   store the request body under <key>
//   GET    /<key>   return the object body (404 when missing)
//   HEAD   /<key>   200 with X-Object-Size + ETag headers (404 when missing)
//   DELETE /<key>   delete the object
//   POST   /__copy  JSON {"src": "<key>", "dst": "<key>"} server-side copy
export default {
  async fetch(request, env) {
    const token = env.GATEWAY_TOKEN;
    const auth = request.headers.get("Authorization") || "";
    if (!token || auth !== `Bearer ${token}`) {
      return new Response("unauthorized", { status: 401 });
    }

    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/__copy") {
      let payload;
      try {
        payload = await request.json();
      } catch {
        return new Response("bad json", { status: 400 });
      }
      const { src, dst } = payload || {};
      if (!src || !dst) {
        return new Response("src and dst are required", { status: 400 });
      }
      const obj = await env.DB.get(src);
      if (!obj) {
        return new Response("src not found", { status: 404 });
      }
      await env.DB.put(dst, obj.body);
      return new Response("ok", { status: 200 });
    }

    const key = decodeURIComponent(url.pathname.replace(/^\//, ""));
    if (!key) {
      return new Response("no key", { status: 400 });
    }

    switch (request.method) {
      case "PUT":
        await env.DB.put(key, request.body);
        return new Response("ok", { status: 200 });
      case "GET": {
        const obj = await env.DB.get(key);
        if (!obj) {
          return new Response("not found", { status: 404 });
        }
        return new Response(obj.body, { status: 200 });
      }
      case "HEAD": {
        const obj = await env.DB.head(key);
        if (!obj) {
          return new Response("not found", { status: 404 });
        }
        // The Workers runtime ignores a user-set Content-Length, so the
        // size travels in a custom header. ETag is R2's httpEtag (the
        // object MD5 for non-multipart uploads) for content checks.
        return new Response(null, {
          status: 200,
          headers: {
            "X-Object-Size": String(obj.size),
            "ETag": obj.httpEtag,
          },
        });
      }
      case "DELETE":
        await env.DB.delete(key);
        return new Response(null, { status: 204 });
      default:
        return new Response("method not allowed", { status: 405 });
    }
  },
};
