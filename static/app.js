const API = "";
const MAX_TEXT_LENGTH = 150;

const DEFAULT_PET = {
  pet_id: "pet_demo_001",
  pet_name: "警长",
  species: "cat",
  breed: "英短",
  age_years: 4,
  weight_kg: 5.2,
  sex: "公",
};

const checks = {
  dental: {
    eyebrow: "DENTAL CHECK",
    name: "牙科评估",
    description: "拍摄牙齿与牙龈的清晰照片，观察牙结石、牙龈状态、口腔清洁度并获得洁牙建议。",
    tips: ["自然光下拍摄，避免闪光反射", "尽量露出左右两侧后槽牙", "照片清晰、无遮挡"],
    title: "上传口腔照片",
    subtitle: "点击选择或拖放图片",
    accept: "image/jpeg,image/png,image/webp",
  },
  stool: {
    eyebrow: "STOOL CHECK",
    name: "便便分析",
    description: "通过便便照片观察颜色、形态和质地，帮助你理解近期消化状态与需要持续记录的信号。",
    tips: ["拍摄完整便便，保留比例参照", "使用自然光，避免滤镜和色偏", "记录排便次数与伴随症状"],
    title: "上传便便照片",
    subtitle: "点击选择或拖放图片",
    accept: "image/jpeg,image/png,image/webp",
  },
  gait: {
    eyebrow: "GAIT CHECK",
    name: "步态分析",
    description: "上传连续行走视频，分析步伐节律、四肢协调性和可能出现异常信号的时间点。",
    tips: ["从正面、侧面拍摄完整身体", "保持镜头稳定并覆盖连续行走", "避免在湿滑或过软地面拍摄"],
    title: "上传行走视频",
    subtitle: "支持 MP4、MOV，时长 5–15 秒",
    accept: "video/mp4,video/quicktime",
  },
  behavior: {
    eyebrow: "BEHAVIOR CHECK",
    name: "行为评估",
    description: "通过日常行为视频观察情绪、压力水平、重复行为与其他值得关注的变化。",
    tips: ["在熟悉环境中自然拍摄", "保留行为发生前后的完整片段", "不要为了拍摄强行诱导异常行为"],
    title: "上传行为视频",
    subtitle: "支持 MP4、MOV，时长 5–15 秒",
    accept: "video/mp4,video/quicktime",
  },
  xray: {
    eyebrow: "XRAY REVIEW",
    name: "X 光片解读",
    description: "上传清晰的 X 光片照片或 PDF，辅助理解影像信息、骨骼与关节结构以及软组织表现。",
    tips: ["拍摄完整影像与方向标记", "画面平整，无明显反光和透视变形", "原始影像仍应交由专业兽医判读"],
    title: "上传 X 光片或 PDF",
    subtitle: "支持 JPG、PNG、WEBP、PDF",
    accept: "image/jpeg,image/png,image/webp,application/pdf",
  },
};

const state = {
  pet: loadJSON("fura.pet", DEFAULT_PET),
  mode: localStorage.getItem("fura.mode") || "real",
  conversationId: localStorage.getItem("fura.conversation") || null,
  lastSequence: 0,
  activeCategory: "dental",
  reportFile: null,
  homeFile: null,
  streaming: false,
  pendingResultId: null,
  pendingResultTitle: null,
  historyPet: "all",
  historyItems: [],
  petProfiles: loadJSON("fura.petProfiles", []),
  deletedPetProfiles: loadJSON("fura.deletedPetProfiles", []),
  editingPetKey: null,
  conversationPromise: null,
  uploadQuality: { report: "smart", home: "smart" },
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function loadJSON(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; }
}

function escapeHTML(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function petProfileKey(pet = {}) {
  const id = pet.pet_id || "no-id";
  // 新档案使用唯一 ID 作为稳定身份；旧版本的共享 demo ID 需要结合
  // 名称、物种和品种拆分，才能修复已经混在一起的多宠物历史。
  if (id !== "no-id" && id !== "pet_demo_001") return `id::${id}`;
  return ["legacy", id, pet.pet_name || "宠物", pet.species || "other", pet.breed || ""].join("::");
}

function mergePetProfiles(pets = []) {
  const profiles = new Map();
  [...state.petProfiles, ...pets, state.pet].forEach((pet) => {
    const key = petProfileKey(pet);
    const deleted = state.deletedPetProfiles.includes(key);
    if (pet?.pet_name && (!deleted || samePetSnapshot(pet, state.pet))) profiles.set(key, { ...pet });
  });
  state.petProfiles = [...profiles.values()];
  localStorage.setItem("fura.petProfiles", JSON.stringify(state.petProfiles));
}

function restorePetProfile(pet) {
  const key = petProfileKey(pet);
  state.deletedPetProfiles = state.deletedPetProfiles.filter((item) => item !== key);
  localStorage.setItem("fura.deletedPetProfiles", JSON.stringify(state.deletedPetProfiles));
  mergePetProfiles([pet]);
}

function petMeta() {
  const species = state.pet.species === "dog" ? "犬" : state.pet.species === "cat" ? "猫" : "宠物";
  const breed = state.pet.breed || species;
  const age = state.pet.age_years !== undefined && state.pet.age_years !== null && state.pet.age_years !== "" ? ` · ${state.pet.age_years}岁` : "";
  return `${breed}${age}`;
}

function renderPet() {
  mergePetProfiles([state.pet]);
  const initial = (state.pet.pet_name || "宠").slice(0, 1);
  $("#sidebar-pet-avatar").textContent = initial;
  $("#sidebar-pet-name").textContent = state.pet.pet_name || "我的宠物";
  $("#sidebar-pet-meta").textContent = petMeta();
  $("#mobile-pet-profile").textContent = initial;
  $("#chat-title").textContent = `${state.pet.pet_name || "宠物"}的 AI 宠物管家`;
  const homeName = $("#home-pet-name");
  const homeMeta = $("#home-pet-meta");
  const insightName = $("#insight-pet-name");
  if (homeName) homeName.textContent = state.pet.pet_name || "它";
  if (homeMeta) homeMeta.textContent = petMeta();
  if (insightName) insightName.textContent = state.pet.pet_name || "它";
  const historyPetName = $("#history-pet-name");
  const historyPetAvatar = $("#history-pet-avatar");
  if (historyPetName) historyPetName.textContent = state.pet.pet_name || "当前宠物";
  if (historyPetAvatar) historyPetAvatar.textContent = initial;
  const welcomeTitle = $(".welcome-message h2");
  if (welcomeTitle) welcomeTitle.textContent = `今天想和我聊聊${state.pet.pet_name || "它"}的什么？`;
  updateModeButton();
}

function updateModeButton() {
  const button = $("#mode-toggle");
  button.querySelector("span").textContent = state.mode === "real" ? "Fura-AI管家" : "演示模式";
  button.classList.toggle("real", state.mode === "real");
}

function setView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$('[data-view]').forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    if (button.classList.contains("nav-item")) button.setAttribute("aria-current", active ? "page" : "false");
  });
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "history") void loadHistory();
}

function toast(message, type = "info") {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show ${type === "error" ? "error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => node.classList.remove("show"), 3200);
}

async function request(url, options = {}) {
  const response = await fetch(`${API}${url}`, options);
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try { detail = (await response.json()).detail || detail; } catch { /* ignore */ }
    if (detail && typeof detail === "object") {
      const error = new Error(detail.user_message || `请求失败（${response.status}）`);
      error.analysisFailure = detail;
      throw error;
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function ensureConversation() {
  if (state.conversationPromise) return state.conversationPromise;
  state.conversationPromise = (async () => {
  if (state.conversationId) {
    try {
      const conversation = await request(`/v1/conversations/${state.conversationId}`);
      // 会话必须绑定当前档案快照。仅检查 conversationId 会把更换档案
      // 后的新消息继续写入旧宠物的历史，并造成“旧记录被改名”的错觉。
      if (samePetSnapshot(conversation.pet, state.pet)) return state.conversationId;
    } catch {
      // 旧会话不存在时按新会话继续创建。
    }
    state.conversationId = null;
    localStorage.removeItem("fura.conversation");
  }
  const conversation = await request("/v1/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: "local_user", pet: state.pet, mode: state.mode }),
  });
  state.conversationId = conversation.conversation_id;
  state.lastSequence = 0;
  localStorage.setItem("fura.conversation", state.conversationId);
  return state.conversationId;
  })();
  try {
    return await state.conversationPromise;
  } finally {
    state.conversationPromise = null;
  }
}

function samePetSnapshot(left, right) {
  const keys = ["pet_id", "pet_name", "species", "breed", "age_years", "weight_kg", "sex"];
  return keys.every((key) => (left?.[key] ?? null) === (right?.[key] ?? null));
}

function messageNode(role, text = "", streaming = false) {
  const article = document.createElement("article");
  article.className = `message ${role}${streaming ? " streaming" : ""}`;
  article.innerHTML = role === "assistant"
    ? `<div class="assistant-avatar">F</div><div class="message-body"><div class="bubble"><p></p></div><time>刚刚</time></div>`
    : `<div class="message-body"><div class="bubble"><p></p></div><time>刚刚</time></div>`;
  article.querySelector("p").textContent = text;
  $("#messages").append(article);
  article.scrollIntoView({ behavior: "smooth", block: "end" });
  return article;
}

function highlightedHTML(content = "") {
  return escapeHTML(content).replace(/\[\[([^\[\]]+)\]\]/g, '<mark class="medical-highlight">$1</mark>');
}

function renderStructuredReply(article, payload, resultId = null) {
  const reply = payload?.reply || {};
  const segments = reply.segments || [];
  const questions = reply.suggested_questions || [];
  const bubble = article.querySelector(".bubble");
  bubble.innerHTML = `<div class="structured-reply">${segments.map((segment) => {
    if (segment.type === "section_title") return `<h4>${escapeHTML(segment.content)}</h4>`;
    const className = segment.type === "list_item" ? "structured-finding" : segment.type === "suggestion_item" ? "structured-suggestion" : "structured-text";
    return `<div class="${className}">${highlightedHTML(segment.content)}</div>`;
  }).join("")}${questions.length ? `<div class="suggested-questions"><small>你还可以继续问</small>${questions.map((question) => `<button type="button">${escapeHTML(question)}</button>`).join("")}</div>` : ""}</div>`;
  bubble.querySelectorAll(".suggested-questions button").forEach((button) => {
    button.addEventListener("click", () => sendMessage(button.textContent, resultId));
  });
}

function setResultContext(resultId = null, title = null) {
  state.pendingResultId = resultId;
  state.pendingResultTitle = title;
  const context = $("#result-context");
  context.hidden = !resultId;
  context.querySelector("strong").textContent = title || "检测结果";
}

function setComposerBusy(busy) {
  state.streaming = busy;
  $(".send-button").disabled = busy;
  $("#chat-input").disabled = busy;
}

async function sendMessage(text, linkedResultId = state.pendingResultId) {
  const normalized = String(text || "").replace(/[\r\n]+/g, " ").replace(/\s{2,}/g, " ").trim();
  if (!normalized || state.streaming) return;
  if (normalized.length > MAX_TEXT_LENGTH) return toast("每条消息最多 150 字", "error");
  $("#quick-prompts").hidden = true;
  const userMessage = messageNode("user", normalized);
  const assistant = messageNode("assistant", "", true);
  const paragraph = assistant.querySelector("p");
  setComposerBusy(true);

  try {
    const conversationId = await ensureConversation();
    const accepted = await request(`/v1/conversations/${conversationId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: normalized,
        client_message_id: crypto.randomUUID ? crypto.randomUUID() : `msg_${Date.now()}`,
        reply_to_result_id: linkedResultId || null,
      }),
    });
    if (linkedResultId === state.pendingResultId) setResultContext();
    streamRun(conversationId, accepted.run_id, assistant, paragraph, linkedResultId, normalized);
  } catch (error) {
    renderChatFailure(assistant, userMessage, normalized, error);
    setComposerBusy(false);
  }
}

function friendlyChatError(error) {
  const detail = String(error?.message || "");
  if (/timeout|超时/i.test(detail)) return "响应超时，本次回复未能完成，请重试。";
  if (/network|fetch|连接/i.test(detail)) return "消息未能发送，请检查网络后重试。";
  return "健康管家暂时无法完成回复，请稍后重试。";
}

function renderChatFailure(assistant, userMessage, originalText, error) {
  assistant.classList.remove("streaming");
  assistant.classList.add("error");
  const bubble = assistant.querySelector(".bubble");
  bubble.innerHTML = `<p>${escapeHTML(friendlyChatError(error))}</p><button class="message-retry" type="button">重新发送</button>`;
  bubble.querySelector("button").onclick = () => {
    assistant.remove();
    userMessage?.remove();
    void sendMessage(originalText);
  };
}

function streamRun(conversationId, runId, assistant, paragraph, linkedResultId = null, originalText = "") {
  let completeText = "";
  const structuredSegments = [];
  let suggestedQuestions = [];
  const source = new EventSource(`${API}/v1/conversations/${conversationId}/events?after_sequence=${state.lastSequence}`);
  const thinkingTimer = setTimeout(() => { if (assistant.classList.contains("streaming")) paragraph.textContent = "管家正在思考中…"; }, 10_000);
  const slowTimer = setTimeout(() => { if (assistant.classList.contains("streaming")) paragraph.textContent = "回复时间较长，管家还在整理与你宠物相关的信息，请稍候。"; }, 15_000);
  const clearThinkingTimers = () => { clearTimeout(thinkingTimer); clearTimeout(slowTimer); };
  const parse = (event) => {
    const payload = JSON.parse(event.data);
    state.lastSequence = Math.max(state.lastSequence, Number(payload.sequence || event.lastEventId || 0));
    return payload;
  };

  source.addEventListener("token.delta", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    completeText += payload.data.delta || "";
    paragraph.textContent = completeText;
    assistant.scrollIntoView({ behavior: "smooth", block: "end" });
  });
  source.addEventListener("structured.segment", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    structuredSegments[payload.data.index] = payload.data.segment;
    renderStructuredReply(assistant, { reply: { segments: structuredSegments.filter(Boolean), suggested_questions: suggestedQuestions } }, linkedResultId);
    assistant.scrollIntoView({ behavior: "smooth", block: "end" });
  });
  source.addEventListener("structured.suggested_questions", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    suggestedQuestions = payload.data.questions || [];
    renderStructuredReply(assistant, { reply: { segments: structuredSegments.filter(Boolean), suggested_questions: suggestedQuestions } }, linkedResultId);
  });
  source.addEventListener("message.completed", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    completeText = payload.data.text || completeText;
    if (payload.data.structured_reply) renderStructuredReply(assistant, payload.data.structured_reply, linkedResultId);
    else paragraph.textContent = completeText;
    assistant.classList.remove("streaming");
  });
  source.addEventListener("run.completed", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    source.close();
    clearThinkingTimers();
    assistant.classList.remove("streaming");
    setComposerBusy(false);
    $("#chat-input").focus();
  });
  source.addEventListener("run.failed", (event) => {
    const payload = parse(event);
    if (payload.run_id !== runId) return;
    source.close();
    clearThinkingTimers();
    renderChatFailure(assistant, assistant.previousElementSibling, originalText, new Error(payload.data.error || "回复生成失败"));
    setComposerBusy(false);
  });
  source.onerror = async () => {
    source.close();
    clearThinkingTimers();
    try {
      const run = await request(`/v1/runs/${runId}`);
      if (run.status === "completed") {
        const messages = await request(`/v1/conversations/${conversationId}/messages`);
        const finalMessage = messages.filter((item) => item.run_id === runId && item.role === "assistant").at(-1);
        if (finalMessage?.structured_reply) renderStructuredReply(assistant, finalMessage.structured_reply, linkedResultId);
        else paragraph.textContent = finalMessage?.text || completeText;
        assistant.classList.remove("streaming");
      } else if (run.status === "failed") {
        throw new Error(run.error || "回复生成失败");
      }
    } catch (error) {
      renderChatFailure(assistant, assistant.previousElementSibling, originalText, error);
    } finally {
      setComposerBusy(false);
    }
  };
}

function fileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function videoDuration(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.preload = "metadata";
    video.onloadedmetadata = () => {
      const duration = Number(video.duration);
      URL.revokeObjectURL(url);
      if (Number.isFinite(duration) && duration > 0) resolve(duration);
      else reject(new Error("视频文件无法打开，可能已损坏"));
    };
    video.onerror = () => { URL.revokeObjectURL(url); reject(new Error("视频文件无法打开，可能已损坏")); };
    video.src = url;
  });
}

async function inspectPdf(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const text = new TextDecoder("latin1").decode(bytes);
  if (!text.slice(0, 4096).includes("%PDF-")) throw new Error("PDF 文件无法打开，可能已损坏");
  if (/\/Encrypt\b/.test(text)) throw new Error("PDF 已加密，请先取消密码保护后重新上传");
  return Math.max(1, (text.match(/\/Type\s*\/Page\b/g) || []).length);
}

async function compressImage(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, 2560 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const blob = await new Promise((resolve, reject) => canvas.toBlob((value) => value ? resolve(value) : reject(new Error("图片智能处理失败")), "image/jpeg", .86));
  return new File([blob], file.name.replace(/\.[^.]+$/, "") + "-optimized.jpg", { type: "image/jpeg", lastModified: Date.now() });
}

function setUploadQuality(scope, quality, clearFile = true) {
  state.uploadQuality[scope] = quality;
  const group = $(`[data-quality-scope="${scope}"]`);
  group?.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item.dataset.quality === quality));
  if (!clearFile) return;
  if (scope === "report") state.reportFile = null; else state.homeFile = null;
  $(`#${scope}-file`).value = "";
  renderSelectedFile(scope, null);
}

function renderSelectedFile(scope, file) {
  const selected = $(`#${scope}-selected`);
  if (!file) {
    selected.hidden = true;
    selected.innerHTML = "";
    $(`#${scope}-analyze`).disabled = true;
    return;
  }
  const preview = URL.createObjectURL(file);
  const media = file.type === "application/pdf"
    ? `<div class="pdf-preview" aria-hidden="true">PDF</div>`
    : file.type.startsWith("video/")
      ? `<video src="${preview}" muted></video>`
      : `<img src="${preview}" alt="已选择素材预览" />`;
  selected.innerHTML = `${media}<div><strong>${escapeHTML(file.name)}</strong><small>${fileSize(file.size)} · 已准备分析</small></div><button type="button" aria-label="移除文件">×</button>`;
  selected.hidden = false;
  $(`#${scope}-analyze`).disabled = false;
  selected.querySelector("button").addEventListener("click", () => {
    if (scope === "report") state.reportFile = null; else state.homeFile = null;
    $(`#${scope}-file`).value = "";
    renderSelectedFile(scope, null);
    URL.revokeObjectURL(preview);
  });
}

function bindDropZone(scope) {
  const zone = $(`#${scope}-drop-zone`);
  const input = $(`#${scope}-file`);
  const keep = (file) => {
    if (scope === "report") state.reportFile = file; else state.homeFile = file;
    renderSelectedFile(scope, file);
  };
  const choose = async (file, shortConfirmed = false) => {
    if (!file) return;
    const reportTypes = new Set(["image/jpeg", "image/png", "image/webp", "application/pdf"]);
    if (scope === "report" && !reportTypes.has(file.type) && !file.name.toLowerCase().endsWith(".pdf")) {
      return showConfirm({ title: "文件无法使用", message: "报告检测仅支持 JPG、PNG、WEBP 或 PDF。", primaryLabel: "重新选择" });
    }
    const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
    const isVideo = file.type.startsWith("video/") || /\.(mp4|mov)$/i.test(file.name);
    const isImage = file.type.startsWith("image/");
    if (scope === "home") {
      const expectsVideo = ["gait", "behavior"].includes(state.activeCategory);
      if (expectsVideo && !isVideo) return showConfirm({ title: "无法用于当前检测", message: "当前项目需要 MP4 或 MOV 视频，请重新选择符合要求的素材。", primaryLabel: "重新选择" });
      if (!expectsVideo && isVideo) return showConfirm({ title: "无法用于当前检测", message: "未检测到符合当前检测分类的有效图片，请重新选择。", primaryLabel: "重新选择" });
      if (isPdf && state.activeCategory !== "xray") return showConfirm({ title: "无法用于当前检测", message: "PDF 仅支持 X 光片解读，请重新选择图片。", primaryLabel: "重新选择" });
      if (!isPdf && !isVideo && !isImage) return showConfirm({ title: "文件格式不支持", message: "当前文件无法用于居家检测，请重新选择。", primaryLabel: "重新选择" });
    }
    if (isPdf) {
      const limit = scope === "home" && state.activeCategory === "xray" ? 20 : 50;
      if (file.size > limit * 1024 * 1024) return showConfirm({ title: "PDF 超出上传上限", message: `当前 PDF 为 ${fileSize(file.size)}，最大支持 ${limit}MB。`, primaryLabel: "重新选择" });
      try {
        const pages = await inspectPdf(file);
        if (scope === "report" && pages > 10) return showConfirm({ title: "PDF 页数超出限制", message: `当前报告共 ${pages} 页，最多支持 10 页，请拆分后重新上传。`, primaryLabel: "重新选择" });
        if (scope === "home" && state.activeCategory === "xray" && pages > 1) {
          return showConfirm({ title: "将分析第一页", message: "已提取第一页影像进行分析，如需分析其他页，请分别上传。", primaryLabel: "我知道了", onConfirm: () => keep(file) });
        }
      } catch (error) {
        return showConfirm({ title: "PDF 无法使用", message: error.message, primaryLabel: "重新选择" });
      }
      keep(file);
      return;
    }
    if (isVideo) {
      if (!/^(video\/mp4|video\/quicktime)$/i.test(file.type) && !/\.(mp4|mov)$/i.test(file.name)) return showConfirm({ title: "视频格式不支持", message: "仅支持 MP4 或 MOV 视频，请重新选择。", primaryLabel: "重新选择" });
      const quality = state.uploadQuality[scope];
      const limit = quality === "smart" ? 100 : 50;
      if (file.size > limit * 1024 * 1024) {
        if (quality === "original" && file.size <= 100 * 1024 * 1024) return showConfirm({ title: "原视频超出上传上限", message: `当前原视频为 ${fileSize(file.size)}，原始质量上限为 50MB。可改用智能压缩，或裁剪后重新选择。`, primaryLabel: "使用智能压缩", onConfirm: () => { setUploadQuality(scope, "smart", false); return choose(file); } });
        return showConfirm({ title: "视频超出素材上限", message: `当前视频为 ${fileSize(file.size)}，超过 100MB 素材上限，请先裁剪。`, primaryLabel: "重新选择" });
      }
      try {
        const duration = await videoDuration(file);
        if (duration > 15.05) return showConfirm({ title: "视频时长超出限制", message: "视频时长超过 15 秒，请选择更短的片段，或裁剪后重新上传。", primaryLabel: "重新选择" });
        if (duration < 5 && !shortConfirmed) return showConfirm({ title: "视频时长较短", message: "建议录制 5 秒以上，以获得更准确的分析结果。", primaryLabel: "继续使用", onConfirm: () => choose(file, true) });
      } catch (error) {
        return showConfirm({ title: "视频无法使用", message: error.message, primaryLabel: "重新选择" });
      }
      keep(file);
      return;
    }
    if (isImage && state.uploadQuality[scope] === "smart") {
      try { file = await compressImage(file); } catch (error) { return showConfirm({ title: "图片无法处理", message: error.message, primaryLabel: "重新选择" }); }
    }
    if (file.size > 50 * 1024 * 1024) return showConfirm({ title: "原图超出上传上限", message: `当前文件为 ${fileSize(file.size)}，模型输入上限为 50MB。`, primaryLabel: "重新选择" });
    keep(file);
  };
  input.addEventListener("change", () => void choose(input.files[0]));
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add("dragging"); }));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove("dragging"); }));
  zone.addEventListener("drop", (event) => void choose(event.dataTransfer.files[0]));
}

function updateHomeCategory(category) {
  state.activeCategory = category;
  const config = checks[category];
  $$(".check-category").forEach((button) => {
    const active = button.dataset.category === category;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  $("#check-eyebrow").textContent = config.eyebrow;
  $("#check-name").textContent = config.name;
  $("#check-description").textContent = config.description;
  $("#check-tips").innerHTML = config.tips.map((tip) => `<li>${escapeHTML(tip)}</li>`).join("");
  $("#home-upload-title").textContent = config.title;
  $("#home-upload-subtitle").textContent = config.subtitle;
  $("#home-file").accept = config.accept;
  $("#home-analyze").childNodes[0].textContent = `开始${config.name} `;
  state.homeFile = null;
  $("#home-file").value = "";
  renderSelectedFile("home", null);
}

function petFormData(formData) {
  return {
    pet_name: formData.get("pet_name") || "我的宠物",
    species: formData.get("species") || "other",
    breed: formData.get("breed") || null,
    age_years: formData.get("age_years") ? Number(formData.get("age_years")) : null,
    weight_kg: formData.get("weight_kg") ? Number(formData.get("weight_kg")) : null,
    sex: formData.get("sex") || null,
  };
}

function petIdentityChanged(previous, next) {
  // 名字、物种、品种或性别发生变化，按“更换宠物”处理；年龄和体重
  // 属于同一宠物的可变档案信息，不会无意义地拆分历史。
  return ["pet_name", "species", "breed", "sex"].some((key) => (previous?.[key] ?? null) !== (next?.[key] ?? null));
}

async function analyze(scope) {
  const file = scope === "report" ? state.reportFile : state.homeFile;
  if (!file) return;
  const category = scope === "report" ? "general" : state.activeCategory;
  const name = scope === "report" ? "报告智能解读" : checks[category].name;
  const analyzeButton = scope === "report" ? $("#report-analyze") : $("#home-analyze");
  const idleLabel = analyzeButton.innerHTML;
  analyzeButton.disabled = true;
  analyzeButton.innerHTML = "正在分析，请稍候…";
  $("#loading-title").textContent = `正在进行${name}`;
  $("#loading-message").textContent = "媒体校验、完整视频理解、专业 Skill 与结果验证正在依次执行；视频超时会自动重试一次，通常需要 1–5 分钟…";
  $("#loading-overlay").hidden = false;
  const body = new FormData();
  body.append("file", file);
  body.append("mode", state.mode);
  body.append("upload_quality", state.uploadQuality[scope]);
  Object.entries(state.pet).forEach(([key, value]) => {
    if (["pet_id", "pet_name", "species", "breed"].includes(key) && value !== null && value !== undefined) body.append(key, value);
  });
  try {
    const conversationId = await ensureConversation();
    body.append("conversation_id", conversationId);
    const endpoint = scope === "report"
      ? `/v1/analysis/report/general/upload`
      : `/v1/analysis/home-check/${category}/upload`;
    const response = await request(endpoint, { method: "POST", body });
    renderResult(response.result, scope, response.result_id);
    openResultModal();
    toast(`${name}已完成`);
  } catch (error) {
    renderAnalysisError(error, scope, name);
    openResultModal();
    toast(error.message, "error");
  } finally {
    $("#loading-overlay").hidden = true;
    analyzeButton.innerHTML = idleLabel;
    analyzeButton.disabled = false;
  }
}

function openResultModal() {
  const modal = $("#result-modal");
  if (!modal.open) modal.showModal();
}

function friendlyAnalysisError(error) {
  if (error?.analysisFailure?.user_message) return error.analysisFailure.user_message;
  const detail = String(error?.message || "分析服务暂时不可用");
  if (/MEDIA_COMPRESSION_FAILED/i.test(detail)) {
    return "智能压缩未能生成可分析的视频。已保留原视频，请裁剪为 5–15 秒后重试。";
  }
  if (/MEDIA_TOO_LONG/i.test(detail)) {
    return "智能压缩仅支持 5–15 秒的视频，请先裁剪后重新上传。";
  }
  if (/MEDIA_UNREADABLE/i.test(detail)) {
    return "无法读取该视频，请导出为 MP4 或 MOV 后重新上传。";
  }
  if (detail.includes("OutputValidationError")) {
    return "模型已完成识别，但结构化结果未通过质量校验。请保留当前文件并重新分析；系统会继续按完整 Skill 纠偏。";
  }
  if (/timeout|超时/i.test(detail)) return "本次分析用时较长，已为你保留素材。你可以直接重新分析。";
  if (/network|fetch|连接/i.test(detail)) return "上传中断，请检查网络后重试。已选择的素材会为你保留。";
  if (/不合规|moder|policy|inappropriate/i.test(detail)) return "上传的素材包含不合规内容，请更换后重试。";
  if (/不是宠物|not.?pet/i.test(detail)) return "当前报告似乎不是宠物检测报告，请上传正确报告。";
  if (/识别|有效内容|unrecogn|MEDIA_UNREADABLE/i.test(detail)) return "未检测到符合当前检测分类的有效内容，请重新选择。";
  return "当前分析服务暂时不可用，请稍后再试。已上传的素材不会丢失。";
}

function renderAnalysisError(error, scope, name) {
  const message = friendlyAnalysisError(error);
  const failure = error?.analysisFailure || {};
  const nextStep = failure.suggestion || "请检查网络后重新分析。若再次失败，错误会继续显示在这里，不会静默关闭。";
  const stage = failure.stage ? ` · ${failure.stage}` : "";
  $("#result-content").innerHTML = `
    <div class="result-wrap result-error-wrap">
      <div class="result-head"><div><span class="feature-kicker">ANALYSIS INTERRUPTED</span><h2>分析未完成</h2><p>${escapeHTML(name)} · 当前素材已保留${escapeHTML(stage)}</p></div><button class="result-close" aria-label="关闭">×</button></div>
      <div class="severity-card result-error-card"><span class="severity high">需重试</span><p>${escapeHTML(message)}</p></div>
      <section class="result-section"><h3>下一步</h3><p class="result-error-help">${escapeHTML(nextStep)}</p></section>
      <div class="result-actions"><button class="ghost-button result-close-secondary">关闭</button><button class="primary-button" id="retry-analysis">重新分析</button></div>
    </div>`;
  $$(".result-close, .result-close-secondary").forEach((button) => button.addEventListener("click", () => $("#result-modal").close()));
  $("#retry-analysis").addEventListener("click", () => {
    $("#result-modal").close();
    analyze(scope);
  });
}

function severityClass(severity) {
  if (severity === "严重") return "high";
  if (severity === "中度") return "medium";
  return "low";
}

function resultTone(uiColor) {
  const color = String(uiColor || "").toLowerCase();
  if (color === "red") return "red";
  if (color === "yellow" || color === "orange") return "orange";
  if (color === "blue") return "blue";
  return "green";
}

function reportStatus(item) {
  const color = resultTone(item.ui_color);
  const detail = `${item.deviation || ""} ${item.item_advice || ""}`;
  if (color === "green") return "正常";
  if (color === "orange") return "临界·需关注";
  if (/偏低|低于|降低/.test(detail)) return "偏低";
  if (/偏高|高于|升高/.test(detail)) return "偏高";
  return "异常";
}

function renderResult(result, scope, resultId) {
  const title = scope === "report" ? (result.report_meta?.report_type || "报告检测") : (result.report_meta?.category_name || "居家检测");
  const summary = result.ai_summary || {};
  const assessmentLabel = summary.assessment_status === "inconclusive"
    ? "建议复测"
    : summary.assessment_status === "partial"
      ? "部分内容待确认"
      : summary.severity || "已完成";
  const runtime = result.report_meta?.analysis_runtime || {};
  const degradedVideo = scope !== "report" && ["degraded_dense_storyboard", "degraded_dense_timeline"].includes(runtime.analysis_quality);
  const denseTimeline = scope !== "report" && runtime.analysis_quality === "high_density_timeline";
  const qualityNotice = degradedVideo
    ? `<div class="analysis-quality-notice"><strong>本次使用全时段顺序帧分析</strong><p>完整视频理解未完成，系统已按时间轴分析 ${escapeHTML(runtime.storyboard_frame_count || 0)} 帧画面。动态结论的置信度低于完整视频分析，建议在结果异常或与日常观察不符时重新检测。</p></div>`
    : denseTimeline
      ? `<div class="analysis-quality-notice"><strong>本次使用高密度时间轴分析</strong><p>原生响应进入长尾或素材超过原生预算，系统已覆盖全时段分析 ${escapeHTML(runtime.storyboard_frame_count || 0)} 帧，并结合原片关键帧复核。</p></div>`
      : "";
  const items = scope === "report"
    ? (result.indicators || []).map((item) => ({
      title: item.full_display,
      value: item.ui_label,
      status: reportStatus(item),
      tone: resultTone(item.ui_color),
      description: item.deviation || (item.ui_color === "Green" ? "当前指标在参考范围内" : "需结合临床表现综合判断"),
      explanation: item.popular_science,
      advice: item.item_advice,
    }))
    : (result.dimensions || []).map((item) => ({
      title: item.title,
      value: item.status_label,
      status: item.status_label || "信息",
      tone: item.status_label ? resultTone(item.ui_color) : "blue",
      description: item.ai_analysis,
      advice: item.suggestion,
    }));
  const suggestions = result.health_suggestions || [];
  $("#result-content").innerHTML = `
    <div class="result-wrap">
      <div class="result-head"><div><span class="feature-kicker">ANALYSIS COMPLETE</span><h2>${escapeHTML(title)}</h2><p>${escapeHTML(state.pet.pet_name || "宠物")} · ${escapeHTML(new Date().toLocaleString("zh-CN", { hour12: false }))}</p></div><button class="result-close" aria-label="关闭">×</button></div>
      ${qualityNotice}
      <div class="severity-card"><span class="severity ${severityClass(summary.severity)}">${escapeHTML(assessmentLabel)}</span><p>${escapeHTML(summary.summary || "分析已完成。")}</p></div>
      <section class="result-section"><h3>${scope === "report" ? "指标详情" : "分项观察"}</h3><div class="result-legend"><span class="tone-green">正常</span><span class="tone-blue">轻微/信息</span><span class="tone-orange">临界/需关注</span><span class="tone-red">异常</span></div><div class="result-items">${items.map((item) => `<article class="result-item result-${item.tone}"><div class="result-item-heading"><strong>${escapeHTML(item.title)}</strong><div class="result-item-meta"><span class="result-status tone-${item.tone}">${escapeHTML(item.status)}</span><span class="result-value">${escapeHTML(item.value || "已分析")}</span></div></div><p class="result-observation">${escapeHTML(item.description || "暂无补充说明")}</p>${item.explanation ? `<p class="result-explanation"><b>AI 解读</b>${escapeHTML(item.explanation)}</p>` : ""}${item.advice ? `<p class="result-advice"><b>建议</b>${escapeHTML(item.advice)}</p>` : ""}</article>`).join("")}</div></section>
      <section class="result-section"><h3>行动建议</h3><div class="suggestions-grid">${suggestions.map((item) => `<article class="suggestion"><small>${escapeHTML(item.ui_label)}</small><strong>${escapeHTML(item.title)}</strong><p>${escapeHTML(item.content)}</p></article>`).join("")}</div></section>
      <p class="result-disclaimer">${escapeHTML(result.disclaimer || "以上分析仅供参考，不构成医疗诊断。如有疑虑，请咨询专业兽医。")}</p>
      <div class="result-actions"><button class="ghost-button result-close-secondary">关闭</button><button class="primary-button" id="ask-about-result">在宠物管家中追问</button></div>
    </div>`;
  $$(".result-close, .result-close-secondary").forEach((button) => button.addEventListener("click", () => $("#result-modal").close()));
  $("#ask-about-result").addEventListener("click", () => {
    $("#result-modal").close();
    setView("chat");
    setResultContext(resultId, title);
    $("#chat-input").value = `请帮我进一步解释刚刚的${title}结果：${summary.summary || ""}`.slice(0, MAX_TEXT_LENGTH);
    $("#input-count").textContent = $("#chat-input").value.length;
    $("#chat-input").focus();
  });
}

function openPetModal() {
  mergePetProfiles([state.pet]);
  setPetFormProfile(state.pet, false);
  $("#pet-modal").showModal();
}

function setPetFormProfile(pet, adding) {
  const form = $("#pet-form");
  form.reset();
  Object.entries(pet || {}).forEach(([key, value]) => {
    if (form.elements[key]) form.elements[key].value = value ?? "";
  });
  state.editingPetKey = adding ? null : petProfileKey(pet);
  $("#pet-modal-title").textContent = adding ? "新增宠物" : `编辑${pet?.pet_name || "宠物"}的档案`;
  $("#save-pet").textContent = adding ? "新增并切换" : "保存并切换";
  renderPetProfileManager();
}

function renderPetProfileManager() {
  const list = $("#pet-profile-list");
  if (!list) return;
  list.innerHTML = state.petProfiles.map((pet) => {
    const key = petProfileKey(pet);
    const current = samePetSnapshot(pet, state.pet);
    const editing = state.editingPetKey === key;
    return `<article class="pet-profile-option ${current ? "current" : ""} ${editing ? "editing" : ""}"><button type="button" class="pet-profile-select" data-pet-profile-key="${escapeHTML(key)}"><span>${escapeHTML((pet.pet_name || "宠").slice(0, 1))}</span><div><strong>${escapeHTML(pet.pet_name || "宠物")}</strong><small>${escapeHTML(petMetaFor(pet))}${current ? " · 当前" : ""}</small></div></button><button type="button" class="pet-profile-remove" data-delete-pet-profile="${escapeHTML(key)}" aria-label="删除${escapeHTML(pet.pet_name || "宠物")}档案">×</button></article>`;
  }).join("");
}

function requestDeletePetProfile(key) {
  const pet = state.petProfiles.find((item) => petProfileKey(item) === key);
  if (!pet) return;
  if (state.petProfiles.length <= 1) {
    toast("至少需要保留一份宠物档案", "error");
    return;
  }
  const deletingCurrent = petProfileKey(state.pet) === key;
  const fallback = state.petProfiles.find((item) => petProfileKey(item) !== key);
  showConfirm({
    title: `删除${pet.pet_name || "宠物"}的档案？`,
    message: `档案将从“我的宠物”中移除${deletingCurrent && fallback ? `，并自动切换至${fallback.pet_name}` : ""}。已保存的对话与检测历史会继续保留。`,
    primaryLabel: "删除档案",
    danger: true,
    onConfirm: () => {
      state.deletedPetProfiles = [...new Set([...state.deletedPetProfiles, key])];
      localStorage.setItem("fura.deletedPetProfiles", JSON.stringify(state.deletedPetProfiles));
      state.petProfiles = state.petProfiles.filter((item) => petProfileKey(item) !== key);
      localStorage.setItem("fura.petProfiles", JSON.stringify(state.petProfiles));
      if (deletingCurrent && fallback) {
        state.pet = { ...fallback };
        localStorage.setItem("fura.pet", JSON.stringify(state.pet));
        state.historyPet = petProfileKey(state.pet);
        resetConversation(false);
        renderPet();
      } else if (state.historyPet === key) {
        state.historyPet = "all";
      }
      setPetFormProfile(state.pet, false);
      toast(`已删除${pet.pet_name}的档案`);
    },
  });
}

function resetConversation(showNotice = true) {
  state.conversationId = null;
  state.lastSequence = 0;
  localStorage.removeItem("fura.conversation");
  setResultContext();
  $$("#messages .message:not(.welcome-message)").forEach((node) => node.remove());
  $("#quick-prompts").hidden = false;
  if (showNotice) toast("已开始一段新对话");
}

function showConfirm({ title, message, primaryLabel = "确认", danger = false, onConfirm }) {
  const modal = $("#confirm-modal");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  const primary = $("#confirm-primary");
  primary.textContent = primaryLabel;
  primary.classList.toggle("danger-button", danger);
  primary.onclick = async () => {
    primary.disabled = true;
    try {
      await onConfirm?.();
      modal.close();
    } catch {
      // The action owns its user-facing error; keep the confirmation open for retry.
    } finally {
      primary.disabled = false;
    }
  };
  $("#confirm-cancel").onclick = () => modal.close();
  if (!modal.open) modal.showModal();
}

function requestNewConversation() {
  showConfirm({
    title: `开始与${state.pet.pet_name || "宠物"}的新对话？`,
    message: `当前对话将保存至${state.pet.pet_name || "宠物"}的历史记录。`,
    primaryLabel: "开始新对话",
    onConfirm: () => {
      resetConversation();
      setView("chat");
    },
  });
}

function historyDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "刚刚" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

function renderHistoryPetFilters(records = []) {
  const container = $("#history-pet-filters");
  if (!container) return;
  mergePetProfiles(records.map((item) => item.pet).filter(Boolean));
  const profiles = state.petProfiles;
  const validKeys = new Set(profiles.map((pet) => petProfileKey(pet)));
  if (state.historyPet !== "all" && !validKeys.has(state.historyPet)) state.historyPet = "all";
  container.innerHTML = [
    `<button class="history-pet-filter ${state.historyPet === "all" ? "active" : ""}" data-history-pet="all"><span>F</span><div><strong>全部记录</strong><small>所有宠物</small></div></button>`,
    ...profiles.map((pet) => {
      const key = petProfileKey(pet);
      const initial = (pet.pet_name || "宠").slice(0, 1);
      const current = samePetSnapshot(pet, state.pet) ? " · 当前" : "";
      return `<button class="history-pet-filter ${state.historyPet === key ? "active" : ""}" data-history-pet="${escapeHTML(key)}"><span>${escapeHTML(initial)}</span><div><strong>${escapeHTML(pet.pet_name || "宠物")}</strong><small>${escapeHTML(petMetaFor(pet))}${current}</small></div></button>`;
    }),
  ].join("");
}

function petMetaFor(pet = {}) {
  const species = pet.species === "dog" ? "犬" : pet.species === "cat" ? "猫" : "宠物";
  const breed = pet.breed || species;
  const age = pet.age_years !== undefined && pet.age_years !== null && pet.age_years !== "" ? ` · ${pet.age_years}岁` : "";
  return `${breed}${age}`;
}

function renderHistoryList(items) {
  const list = $("#history-list");
  if (!items.length) {
    list.innerHTML = `<div class="history-empty"><span>◴</span><strong>还没有对话记录</strong><p>完成一次真实对话后，会自动保存在这里。</p></div>`;
    return;
  }
  list.innerHTML = items.map((item) => `
    <article class="history-row" data-conversation-id="${escapeHTML(item.conversation_id)}">
      <span class="history-row-icon">◴</span>
      <button class="history-row-copy" type="button"><span><strong>${escapeHTML(item.title || "新的对话")}</strong><time>${escapeHTML(historyDate(item.updated_at))}</time></span><p>${escapeHTML(item.summary || "已保存的对话")}</p><small>${escapeHTML(item.pet?.pet_name || "宠物")}</small></button>
      <button class="history-delete" type="button" aria-label="删除对话">•••</button>
    </article>`).join("");
  list.querySelectorAll(".history-row").forEach((row) => {
    const id = row.dataset.conversationId;
    row.querySelector(".history-row-copy").addEventListener("click", () => void openHistoryDetail(id));
    row.querySelector(".history-delete").addEventListener("click", () => requestDeleteConversation(id));
  });
}

async function loadHistory() {
  const status = $("#history-status");
  status.hidden = true;
  $("#history-list").innerHTML = `<div class="history-empty"><span>↻</span><strong>正在读取历史记录</strong><p>请稍候…</p></div>`;
  try {
    const records = await request("/v1/conversations?user_id=local_user");
    const unique = new Map(records.map((item) => [item.conversation_id, item]));
    const allRecords = [...unique.values()];
    renderHistoryPetFilters(allRecords);
    state.historyItems = state.historyPet === "all"
      ? allRecords
      : allRecords.filter((item) => petProfileKey(item.pet) === state.historyPet);
    renderHistoryList(state.historyItems);
  } catch {
    status.hidden = false;
    status.innerHTML = `加载失败，暂时无法读取对话历史。<button type="button" id="history-retry">重新加载</button>`;
    $("#history-list").innerHTML = "";
    $("#history-retry").addEventListener("click", () => void loadHistory());
  }
}

async function openHistoryDetail(conversationId) {
  try {
    const [conversation, messages] = await Promise.all([
      request(`/v1/conversations/${conversationId}`),
      request(`/v1/conversations/${conversationId}/messages?limit=200`),
    ]);
    $("#history-detail-content").innerHTML = `
      <div class="history-detail-wrap">
        <div class="history-detail-head"><div><span class="feature-kicker">SAVED CONVERSATION</span><h2>${escapeHTML(conversation.title)}</h2><p>${escapeHTML(conversation.pet?.pet_name || "宠物")} · ${escapeHTML(historyDate(conversation.updated_at))} · 已保存</p></div><button id="history-detail-close" aria-label="关闭">×</button></div>
        <div class="history-detail-messages">${messages.map((message) => `<article class="history-detail-message ${message.role === "user" ? "user" : "assistant"}"><div><p>${escapeHTML(message.text)}</p><time>${escapeHTML(historyDate(message.created_at))}</time></div></article>`).join("") || `<div class="history-empty"><strong>这段对话还没有消息</strong></div>`}</div>
        <div class="result-actions"><button class="ghost-button" id="history-detail-back">关闭</button><button class="primary-button" id="history-continue">继续这段对话</button></div>
      </div>`;
    const modal = $("#history-detail-modal");
    $("#history-detail-close").onclick = () => modal.close();
    $("#history-detail-back").onclick = () => modal.close();
    $("#history-continue").onclick = async () => {
      modal.close();
      // 继续旧历史时恢复该会话创建时的档案快照，保证后续消息仍写入
      // 同一只宠物，而不是沿用界面上另一只宠物的当前档案。
      if (!samePetSnapshot(state.pet, conversation.pet)) {
        state.pet = { ...conversation.pet };
        restorePetProfile(state.pet);
        localStorage.setItem("fura.pet", JSON.stringify(state.pet));
        renderPet();
      }
      state.conversationId = conversationId;
      state.lastSequence = 0;
      localStorage.setItem("fura.conversation", conversationId);
      await restoreConversationMessages();
      setView("chat");
      $("#chat-input").focus();
    };
    if (!modal.open) modal.showModal();
  } catch {
    toast("历史详情加载失败，请检查网络后重试", "error");
  }
}

function requestDeleteConversation(conversationId) {
  const item = state.historyItems.find((record) => record.conversation_id === conversationId);
  showConfirm({
    title: "删除这条对话记录？",
    message: `删除后该对话记录将不再用于${item?.pet?.pet_name || "这只宠物"}的上下文，且无法恢复。`,
    primaryLabel: "删除",
    danger: true,
    onConfirm: async () => {
      try {
        await request(`/v1/conversations/${conversationId}`, { method: "DELETE" });
        if (state.conversationId === conversationId) resetConversation(false);
        state.historyItems = state.historyItems.filter((record) => record.conversation_id !== conversationId);
        renderHistoryList(state.historyItems);
        toast("已删除这条对话");
      } catch {
        toast("删除失败，记录仍为你保留，请稍后重试", "error");
        throw new Error("delete failed");
      }
    },
  });
}

async function restoreConversationMessages() {
  if (!state.conversationId) return;
  try {
    const messages = await request(`/v1/conversations/${state.conversationId}/messages?limit=200`);
    $$("#messages .message:not(.welcome-message)").forEach((node) => node.remove());
    for (const message of messages) {
      const article = messageNode(message.role === "user" ? "user" : "assistant", message.text);
      if (message.role === "assistant" && message.structured_reply) renderStructuredReply(article, message.structured_reply);
      article.querySelector("time").textContent = historyDate(message.created_at);
    }
    $("#quick-prompts").hidden = messages.length > 0;
  } catch {
    state.conversationId = null;
    localStorage.removeItem("fura.conversation");
  }
}

function bindEvents() {
  $$('[data-view]').forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#chat-input");
    const text = input.value;
    input.value = "";
    $("#input-count").textContent = "0";
    input.style.height = "auto";
    sendMessage(text);
  });
  $("#chat-input").addEventListener("input", (event) => {
    $("#input-count").textContent = String(event.target.value.length);
    event.target.style.height = "auto";
    event.target.style.height = `${Math.min(event.target.scrollHeight, 150)}px`;
  });
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#chat-form").requestSubmit(); }
  });
  $("#result-context button").addEventListener("click", () => setResultContext());
  $("#insight-ask").addEventListener("click", () => {
    const input = $("#chat-input");
    input.value = `请帮我梳理${state.pet.pet_name || "宠物"}今天需要重点观察的饮水、食欲和精神状态。`;
    $("#input-count").textContent = String(input.value.length);
    input.focus();
  });
  $$("#quick-prompts button").forEach((button) => button.addEventListener("click", () => sendMessage(button.textContent)));
  $("#new-conversation").addEventListener("click", requestNewConversation);
  $("#history-new-conversation").addEventListener("click", requestNewConversation);
  $("#history-refresh").addEventListener("click", () => void loadHistory());
  $("#history-pet-filters").addEventListener("click", (event) => {
    const button = event.target.closest("[data-history-pet]");
    if (!button) return;
    state.historyPet = button.dataset.historyPet;
    void loadHistory();
  });
  $$('[data-quality-scope]').forEach((group) => group.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => {
    const scope = group.dataset.qualityScope;
    setUploadQuality(scope, button.dataset.quality);
  })));
  $("#mode-toggle").addEventListener("click", () => {
    state.mode = state.mode === "fake" ? "real" : "fake";
    localStorage.setItem("fura.mode", state.mode);
    updateModeButton();
    resetConversation(false);
    toast(state.mode === "real" ? "已切换 Fura-AI宠物管家服务" : "已切换安全演示模式");
  });
  [$("#open-pet-profile"), $("#mobile-pet-profile")].forEach((button) => button.addEventListener("click", openPetModal));
  $("#add-pet-profile").addEventListener("click", () => setPetFormProfile({ species: "cat", sex: "未知" }, true));
  $("#pet-profile-list").addEventListener("click", (event) => {
    const deleteButton = event.target.closest("[data-delete-pet-profile]");
    if (deleteButton) {
      requestDeletePetProfile(deleteButton.dataset.deletePetProfile);
      return;
    }
    const button = event.target.closest("[data-pet-profile-key]");
    if (!button) return;
    const pet = state.petProfiles.find((item) => petProfileKey(item) === button.dataset.petProfileKey);
    if (pet) setPetFormProfile(pet, false);
  });
  $("#pet-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (event.submitter?.value === "cancel") {
      $("#pet-modal").close();
      return;
    }
    const previous = state.editingPetKey
      ? state.petProfiles.find((item) => petProfileKey(item) === state.editingPetKey)
      : null;
    const next = { ...(previous || {}), ...petFormData(new FormData(event.target)) };
    if (!previous?.pet_id) next.pet_id = globalThis.crypto?.randomUUID?.() ? `pet_${crypto.randomUUID()}` : `pet_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    if (state.editingPetKey) state.petProfiles = state.petProfiles.filter((item) => petProfileKey(item) !== state.editingPetKey);
    state.pet = next;
    state.deletedPetProfiles = state.deletedPetProfiles.filter((item) => item !== petProfileKey(next));
    localStorage.setItem("fura.deletedPetProfiles", JSON.stringify(state.deletedPetProfiles));
    mergePetProfiles([next]);
    state.historyPet = petProfileKey(next);
    localStorage.setItem("fura.pet", JSON.stringify(state.pet));
    renderPet();
    resetConversation(false);
    $("#pet-modal").close();
    toast(previous ? `已切换至${next.pet_name}` : `已新增并切换至${next.pet_name}`);
  });
  $$(".check-category").forEach((button) => button.addEventListener("click", () => updateHomeCategory(button.dataset.category)));
  bindDropZone("report");
  bindDropZone("home");
  $("#report-analyze").addEventListener("click", () => analyze("report"));
  $("#home-analyze").addEventListener("click", () => analyze("home"));
  $("#result-modal").addEventListener("click", (event) => { if (event.target === $("#result-modal")) $("#result-modal").close(); });
}

renderPet();
updateHomeCategory("dental");
bindEvents();
void restoreConversationMessages();
