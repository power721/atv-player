import { createRequire } from "node:module";
import path from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const result = {};
  for (let i = 0; i < argv.length; i += 2) {
    result[argv[i].replace(/^--/, "")] = argv[i + 1];
  }
  return result;
}

const args = parseArgs(process.argv.slice(2));
const pluginPath = args.plugin;
const cacheDir = args["cache-dir"];
const pluginId = args["plugin-id"];

if (!pluginPath || !cacheDir || !pluginId) {
  throw new Error("missing required bridge arguments");
}

function cachePath(rule, key) {
  const safeRule = String(rule || "default").replace(/[^a-zA-Z0-9_.-]/g, "_");
  const safeKey = String(key || "").replace(/[^a-zA-Z0-9_.-]/g, "_");
  return path.join(cacheDir, "js-local", String(pluginId), safeRule, `${safeKey}.txt`);
}

globalThis.local = {
  get(rule, key) {
    try {
      return require("node:fs").readFileSync(cachePath(rule, key), "utf-8");
    } catch {
      return "";
    }
  },
  set(rule, key, value) {
    const file = cachePath(rule, key);
    require("node:fs").mkdirSync(path.dirname(file), { recursive: true });
    require("node:fs").writeFileSync(file, String(value ?? ""), "utf-8");
  },
  delete(rule, key) {
    try {
      require("node:fs").unlinkSync(cachePath(rule, key));
    } catch {
    }
  },
};

async function http(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  return {
    ok: response.ok,
    status: response.status,
    url: response.url,
    headers: Object.fromEntries(response.headers.entries()),
    content: text,
    text,
    json: () => JSON.parse(text),
  };
}

globalThis.http = http;
globalThis.req = http;

function responseHeaders(response) {
  const headers = Object.fromEntries(response.headers.entries());
  const getSetCookie = response.headers.getSetCookie;
  if (typeof getSetCookie === "function") {
    const cookies = getSetCookie.call(response.headers);
    if (cookies.length > 0) headers["set-cookie"] = cookies;
  } else {
    const cookie = response.headers.get("set-cookie");
    if (cookie) headers["set-cookie"] = [cookie];
  }
  return headers;
}

function mergeOptions(defaultOptions = {}, options = {}) {
  return {
    ...defaultOptions,
    ...options,
    headers: {
      ...(defaultOptions.headers || {}),
      ...(options.headers || {}),
    },
  };
}

function transformAxiosData(text, options) {
  let data = text;
  const transforms = Array.isArray(options.transformResponse)
    ? options.transformResponse
    : [];
  for (const transform of transforms) {
    if (typeof transform === "function") {
      data = transform(data);
    }
  }
  if (transforms.length === 0 && options.responseType !== "text") {
    try {
      data = JSON.parse(text);
    } catch {
    }
  }
  return data;
}

function hasHeader(headers, name) {
  const lowerName = name.toLowerCase();
  return Object.keys(headers).some((key) => key.toLowerCase() === lowerName);
}

function serializeAxiosData(data, headers) {
  if (
    data
    && typeof data === "object"
    && !(data instanceof URLSearchParams)
    && !Buffer.isBuffer(data)
    && !(data instanceof ArrayBuffer)
    && !ArrayBuffer.isView(data)
  ) {
    if (!hasHeader(headers, "content-type")) {
      headers["Content-Type"] = "application/json";
    }
    return JSON.stringify(data);
  }
  return data;
}

async function axiosRequest(method, url, data = null, options = {}) {
  const requestMethod = String(method || "GET").toUpperCase();
  const headers = options.headers || {};
  const body = serializeAxiosData(data, headers);
  const timeout = Number(options.timeout || 0);
  const controller = timeout > 0 ? new AbortController() : null;
  const timeoutId = controller
    ? setTimeout(() => controller.abort(), timeout)
    : null;
  try {
    const response = await fetch(url, {
      method: requestMethod,
      headers,
      body: ["GET", "HEAD"].includes(requestMethod) ? undefined : body,
      signal: controller?.signal,
    });
    const text = await response.text();
    const result = {
      data: transformAxiosData(text, options),
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders(response),
    };
    const validateStatus = options.validateStatus;
    if (typeof validateStatus === "function" && !validateStatus(response.status)) {
      const error = new Error(`Request failed with status code ${response.status}`);
      error.response = result;
      throw error;
    }
    return result;
  } finally {
    if (timeoutId) clearTimeout(timeoutId);
  }
}

async function axiosGet(url, options = {}) {
  return axiosRequest("GET", url, null, options);
}

async function axiosPost(url, data = null, options = {}) {
  return axiosRequest("POST", url, data, options);
}

async function axiosFromConfig(defaultOptions, configOrUrl, options = {}) {
  if (typeof configOrUrl === "string") {
    return axiosGet(configOrUrl, mergeOptions(defaultOptions, options));
  }
  const config = mergeOptions(defaultOptions, configOrUrl || {});
  return axiosRequest(
    config.method || "GET",
    config.url,
    config.data ?? null,
    config
  );
}

function createAxiosClient(defaultOptions = {}) {
  const client = (configOrUrl, options = {}) => {
    return axiosFromConfig(defaultOptions, configOrUrl, options);
  };
  client.get = (url, options = {}) => {
    return axiosGet(url, mergeOptions(defaultOptions, options));
  };
  client.post = (url, data = null, options = {}) => {
    return axiosPost(url, data, mergeOptions(defaultOptions, options));
  };
  return client;
}

const axiosShim = createAxiosClient();
axiosShim.create = createAxiosClient;
const cheerioShim = await import(new URL("./lib/cheerio.min.js", import.meta.url));
const cryptoJsShim = require("./lib/crypto-js.js");

function escapeXmlText(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeXmlAttribute(value) {
  return escapeXmlText(value).replaceAll("\"", "&quot;");
}

class XmlBuilderNode {
  constructor(name = null, attributes = {}, parent = null, document = null) {
    this.name = name;
    this.attributes = { ...(attributes || {}) };
    this.parent = parent;
    this.document = document || this;
    this.children = [];
  }

  ele(name, attributes = {}) {
    const child = new XmlBuilderNode(name, attributes, this, this.document);
    this.children.push(child);
    return child;
  }

  att(name, value) {
    if (name && typeof name === "object") {
      Object.assign(this.attributes, name);
    } else {
      this.attributes[name] = value;
    }
    return this;
  }

  txt(value) {
    this.children.push({ text: String(value ?? "") });
    return this;
  }

  up() {
    return this.parent || this;
  }

  end() {
    return renderXmlDocument(this.document);
  }
}

function renderXmlDocument(document) {
  const options = document.declaration || {};
  const declaration = options.version
    ? `<?xml version="${escapeXmlAttribute(options.version)}"${options.encoding ? ` encoding="${escapeXmlAttribute(options.encoding)}"` : ""}?>`
    : "";
  return `${declaration}${document.children.map(renderXmlNode).join("")}`;
}

function renderXmlNode(node) {
  if (Object.prototype.hasOwnProperty.call(node, "text")) {
    return escapeXmlText(node.text);
  }
  const attributes = Object.entries(node.attributes)
    .map(([name, value]) => ` ${name}="${escapeXmlAttribute(value)}"`)
    .join("");
  if (node.children.length === 0) {
    return `<${node.name}${attributes}/>`;
  }
  return `<${node.name}${attributes}>${node.children.map(renderXmlNode).join("")}</${node.name}>`;
}

function xmlbuilderCreate(options = {}) {
  const document = new XmlBuilderNode();
  document.declaration = (
    options
    && typeof options === "object"
    && (options.version || options.encoding)
  )
    ? options
    : {};
  return document;
}

const xmlbuilder2Shim = { create: xmlbuilderCreate };

function uuidV4() {
  const nodeCrypto = require("node:crypto");
  if (typeof nodeCrypto.randomUUID === "function") {
    return nodeCrypto.randomUUID();
  }
  const bytes = nodeCrypto.randomBytes(16);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const uuidShim = { v4: uuidV4 };

const originalConsole = globalThis.console;
globalThis.console = {
  log: (...items) => originalConsole.error(JSON.stringify({ level: "info", message: items.map(String).join(" ") })),
  info: (...items) => originalConsole.error(JSON.stringify({ level: "info", message: items.map(String).join(" ") })),
  warn: (...items) => originalConsole.error(JSON.stringify({ level: "warning", message: items.map(String).join(" ") })),
  error: (...items) => originalConsole.error(JSON.stringify({ level: "error", message: items.map(String).join(" ") })),
};

async function loadPluginModule() {
  const fs = require("node:fs");
  const source = fs.readFileSync(pluginPath, "utf-8");
  const suffix = path.extname(pluginPath).toLowerCase();
  const isCommonJS = suffix === ".cjs" || source.includes("module.exports");
  if (!isCommonJS) {
    return {
      namespace: await import(pathToFileURL(pluginPath).href),
      exported: null,
      source,
      isCommonJS: false,
    };
  }

  const pluginRequireBase = createRequire(pathToFileURL(pluginPath));
  const pluginRequire = (name) => {
    if (name === "axios") return axiosShim;
    if (name === "cheerio") return cheerioShim;
    if (name === "crypto-js") return cryptoJsShim;
    if (name === "xmlbuilder2") return xmlbuilder2Shim;
    if (name === "uuid") return uuidShim;
    return pluginRequireBase(name);
  };
  const module = { exports: {} };
  const wrapper = new Function(
    "require",
    "module",
    "exports",
    "__filename",
    "__dirname",
    source
  );
  wrapper(
    pluginRequire,
    module,
    module.exports,
    pluginPath,
    path.dirname(pluginPath)
  );
  return {
    namespace: { default: module.exports, ...module.exports },
    exported: module.exports,
    source,
    isCommonJS: true,
  };
}

function normalizeResult(value) {
  if (typeof value === "string") {
    try {
      return JSON.parse(value);
    } catch {
      return value;
    }
  }
  return value;
}

function looksLikeT4Registrar(fn, source) {
  if (typeof fn !== "function") return false;
  if (fn.length >= 2) return true;
  return [
    "app.get",
    "app.post",
    "server.get",
    "server.post",
    "fastify.get",
    "fastify.post",
    "opt.sites",
  ].some((marker) => source.includes(marker));
}

async function createT4Spider(register, meta = {}) {
  const routes = [];
  const opt = { sites: [] };
  const app = {
    log: console,
    get(api, handler) {
      routes.push({ api, handler });
    },
    post(api, handler) {
      routes.push({ api, handler });
    },
  };
  await register(app, opt);
  const siteMeta = meta || opt.sites[0] || {};
  const route = (
    routes.find((item) => item.api === siteMeta.api)
    || routes.find((item) => item.api === opt.sites[0]?.api)
    || routes.find((item) => String(item.api || "").startsWith("/video/"))
    || routes[0]
  );
  if (!route || typeof route.handler !== "function") {
    throw new Error("T4 JavaScript plugin did not register a route");
  }

  async function callRoute(query) {
    let sent = false;
    let payload = null;
    const reply = {
      send(value) {
        sent = true;
        payload = value;
        return value;
      },
    };
    const result = await route.handler({ query }, reply);
    return normalizeResult(sent ? payload : result);
  }

  return {
    getName() {
      return siteMeta.name || opt.sites[0]?.name || "";
    },
    home() {
      return callRoute({});
    },
    category(tid, pg, filter, extend) {
      const extendText = JSON.stringify(extend || {});
      const ext = Buffer.from(extendText).toString("base64");
      return callRoute({ t: tid, pg: String(pg || 1), ext, extend: extendText });
    },
    detail(id) {
      return callRoute({ ids: id });
    },
    search(key, quick, pg) {
      return callRoute({ wd: key, pg: String(pg || 1) });
    },
    play(flag, id) {
      return callRoute({ play: id, flag, from: flag });
    },
  };
}

const loadedPlugin = await loadPluginModule();
const pluginModule = loadedPlugin.namespace;
let spider = null;
if (
  loadedPlugin.isCommonJS
  && typeof loadedPlugin.exported === "function"
  && looksLikeT4Registrar(loadedPlugin.exported, loadedPlugin.source)
) {
  spider = await createT4Spider(
    loadedPlugin.exported,
    loadedPlugin.exported.META || pluginModule.META
  );
} else if (
  loadedPlugin.isCommonJS
  && looksLikeT4Registrar(pluginModule.default, loadedPlugin.source)
) {
  spider = await createT4Spider(
    pluginModule.default,
    pluginModule.META || loadedPlugin.exported.META
  );
} else if (typeof pluginModule.__jsEvalReturn === "function") {
  spider = await pluginModule.__jsEvalReturn();
} else if (typeof pluginModule.default === "function") {
  spider = await pluginModule.default();
} else if (pluginModule.default) {
  spider = pluginModule.default;
} else {
  spider = pluginModule;
}

if (!spider || typeof spider !== "object") {
  throw new Error("JavaScript plugin did not export a spider object");
}

async function callMethod(method, args) {
  const map = {
    init: "init",
    home: "home",
    category: "category",
    detail: "detail",
    search: "search",
    play: "play",
    danmakuContent: "danmakuContent",
    getName: "getName",
    destroy: "destroy",
  };
  if (method === "hasMethod") return typeof spider[args[0]] === "function";
  const jsName = map[method];
  const fn = jsName ? spider[jsName] : null;
  if (typeof fn !== "function") {
    if (method === "init" || method === "destroy") return null;
    throw new Error(`${method} is not defined`);
  }
  return normalizeResult(await fn.apply(spider, args));
}

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  try {
    const result = await callMethod(request.method, request.args || []);
    process.stdout.write(`${JSON.stringify({ id: request.id, ok: true, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ id: request.id, ok: false, error: String(error?.message || error) })}\n`);
  }
}
