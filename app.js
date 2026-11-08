const libraryView = document.querySelector("#library-view");
const downloadsView = document.querySelector("#downloads-view");
const settingsView = document.querySelector("#settings-view");
const currentSection = document.querySelector("#current-section");
const navigationItems = document.querySelectorAll(".nav-item");
const api = async (path, options = {}) => {
  let response;
  try {
    response = await fetch(`/api/${path}`, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch {
    throw new Error("无法连接本机启动服务。请运行 python3 launcher.py，并打开终端显示的地址。");
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("本机地址没有启动游戏后端。请运行 python3 launcher.py，并打开终端显示的地址。");
  }
  if (!response.ok) throw new Error(result.error || `本机启动器请求失败（${response.status}）。`);
  return result;
};

function showView(viewName) {
  const views = {
    library: { element: libraryView, title: "游戏库" },
    downloads: { element: downloadsView, title: "下载中心" },
    settings: { element: settingsView, title: "启动设置" },
  };
  const selectedView = views[viewName];
  if (!selectedView) return;

  Object.entries(views).forEach(([name, view]) => {
    view.element.hidden = name !== viewName;
  });
  currentSection.textContent = selectedView.title;
  if (viewName === "downloads" && !manifestLoaded && !manifestLoading) {
    void loadVersionManifest();
  }

  navigationItems.forEach((item) => {
    const isActive = item.dataset.view === viewName;
    item.classList.toggle("is-active", isActive);
    if (isActive) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  });
}

navigationItems.forEach((item) => {
  item.addEventListener("click", () => showView(item.dataset.view));
});

const searchInput = document.querySelector("#game-search");
const gameCard = document.querySelector(".game-card");
const addCard = document.querySelector(".add-card");
const emptySearch = document.querySelector("#empty-search");
const visibleCount = document.querySelector("#visible-count");

searchInput.addEventListener("input", () => {
  const matches = gameCard.dataset.search.includes(searchInput.value.trim().toLocaleLowerCase());
  gameCard.hidden = !matches;
  addCard.hidden = !matches;
  emptySearch.hidden = matches;
  visibleCount.textContent = matches ? "1" : "0";
});

document.addEventListener("keydown", (event) => {
  const activeElement = document.activeElement;
  const isEditingText = activeElement.matches("input, textarea, [contenteditable='true']");
  if (event.key === "/" && !isEditingText && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault();
    searchInput.focus();
  }
});

const settingsKey = "mc-pcl.launch-settings.v1";
const defaultSettings = { memory: 4, jvmArguments: "" };
const settingsForm = document.querySelector("#launcher-settings-form");
const memoryInput = document.querySelector("#memory-limit");
const memoryValue = document.querySelector("#memory-value");
const jvmArgumentsInput = document.querySelector("#jvm-arguments");
const settingsStatus = document.querySelector("#settings-status");
const saveSettingsButton = document.querySelector("#save-settings");
let settingsDirty = false;
let settingsSaving = false;

function setSettingsStatus(message, isError = false) {
  settingsStatus.textContent = message;
  settingsStatus.classList.toggle("is-error", isError);
}

function getSettingsFormValues() {
  const memory = Number(memoryInput.value);
  const jvmArguments = jvmArgumentsInput.value;
  const argumentsList = jvmArguments.split(/\r?\n/).map((argument) => argument.trim()).filter(Boolean);
  const invalidArguments = argumentsList.some((argument) => !argument.startsWith("-") || /\s/.test(argument));

  jvmArgumentsInput.setAttribute("aria-invalid", String(invalidArguments));
  saveSettingsButton.disabled = invalidArguments || settingsSaving;

  return {
    memory,
    jvmArguments,
    invalidArguments,
    validMemory: Number.isInteger(memory) && memory >= 2 && memory <= 16,
  };
}

function loadSettings() {
  let serialized;
  try {
    serialized = localStorage.getItem(settingsKey);
  } catch {
    setSettingsStatus("无法读取本地设置，请检查浏览器存储权限。", true);
    return;
  }
  if (serialized === null) return;

  let savedSettings;
  try {
    savedSettings = JSON.parse(serialized);
  } catch {
    setSettingsStatus("本地设置无法解析；请重新保存或恢复默认设置。", true);
    return;
  }
  if (
    !savedSettings ||
    !Number.isInteger(savedSettings.memory) ||
    savedSettings.memory < 2 ||
    savedSettings.memory > 16 ||
    typeof savedSettings.jvmArguments !== "string"
  ) {
    setSettingsStatus("本地设置格式无效；请重新保存或恢复默认设置。", true);
    return;
  }

  memoryInput.value = String(savedSettings.memory);
  jvmArgumentsInput.value = savedSettings.jvmArguments;
  memoryValue.value = String(savedSettings.memory);
  memoryValue.textContent = String(savedSettings.memory);
  const { invalidArguments } = getSettingsFormValues();
  setSettingsStatus(
    invalidArguments ? "本地 Java 参数格式无效，请修正后保存。" : "已加载本设备保存的设置。",
    invalidArguments,
  );
}

memoryInput.addEventListener("input", () => {
  settingsDirty = true;
  memoryValue.value = memoryInput.value;
  memoryValue.textContent = memoryInput.value;
  setSettingsStatus("有未保存的更改。");
});

jvmArgumentsInput.addEventListener("input", () => {
  settingsDirty = true;
  const { invalidArguments } = getSettingsFormValues();
  setSettingsStatus(invalidArguments ? "参数格式无效：每行需以 - 开头，且不能包含空格。" : "有未保存的更改。", invalidArguments);
});

settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const values = getSettingsFormValues();
  if (values.invalidArguments || !values.validMemory) {
    setSettingsStatus("设置无效，请检查内存和 Java 参数。", true);
    return;
  }

  const settings = {
    memory: values.memory,
    jvmArguments: values.jvmArguments,
  };
  void (async () => {
    settingsSaving = true;
    getSettingsFormValues();
    try {
      await api("settings", { method: "POST", body: JSON.stringify(settings) });
      let browserCacheFailed = false;
      try {
        localStorage.setItem(settingsKey, JSON.stringify(settings));
      } catch {
        browserCacheFailed = true;
      }
      const unchanged = memoryInput.value === String(settings.memory) &&
        jvmArgumentsInput.value === settings.jvmArguments;
      settingsDirty = !unchanged;
      setSettingsStatus(
        unchanged
          ? browserCacheFailed
            ? "设置已保存在本机启动器；浏览器缓存不可用。"
            : "设置已保存在此设备。"
          : "设置已保存；还有新的未保存更改。",
      );
    } catch (error) {
      setSettingsStatus(error.message || "保存失败，请检查本机启动器服务。", true);
    } finally {
      settingsSaving = false;
      getSettingsFormValues();
    }
  })();
});

document.querySelector("#reset-settings").addEventListener("click", () => {
  settingsDirty = true;
  memoryInput.value = String(defaultSettings.memory);
  jvmArgumentsInput.value = defaultSettings.jvmArguments;
  memoryValue.value = String(defaultSettings.memory);
  memoryValue.textContent = String(defaultSettings.memory);
  jvmArgumentsInput.setAttribute("aria-invalid", "false");
  getSettingsFormValues();
  setSettingsStatus("默认设置已恢复，请保存以应用。");
});

loadSettings();

const profileName = document.querySelector("#profile-name");
const profileStatus = document.querySelector("#profile-status");
const accountButton = document.querySelector("#account-button");
const javaStatus = document.querySelector("#java-status");
const installedVersionSelect = document.querySelector("#installed-version-select");
const launchGameButton = document.querySelector("#launch-game");
const gameTaskState = document.querySelector("#game-task-state");
const loginDialog = document.querySelector("#login-dialog");
const startLoginButton = document.querySelector("#start-login");
const logoutButton = document.querySelector("#logout-account");
const loginCodeBlock = document.querySelector("#login-code-block");
const loginUserCode = document.querySelector("#login-user-code");
const loginVerificationLink = document.querySelector("#login-verification-link");
const loginPollStatus = document.querySelector("#login-poll-status");
const loginError = document.querySelector("#login-error");
const clientIdInput = document.querySelector("#microsoft-client-id");
const clientIdStatus = document.querySelector("#client-id-status");
let systemState = null;
let activeInstallTaskId = null;
let activeLaunchTaskId = null;
let downloadBusy = false;

function updateLaunchAvailability() {
  const installed = installedVersionSelect.value;
  const installedVersion = systemState?.installedVersions?.find((version) => version.id === installed);
  const requiredJava = installedVersion?.javaVersion?.majorVersion;
  const javaCandidates = [systemState?.java, ...(systemState?.javaRuntimes || [])].filter(Boolean);
  const hasJava = javaCandidates.some((java) =>
    !Number.isInteger(requiredJava) || java.major >= requiredJava,
  );
  const hasProfile = Boolean(systemState?.profile);
  launchGameButton.disabled = !installed || !hasJava || !hasProfile || Boolean(activeLaunchTaskId || activeInstallTaskId);
  if (!installed) {
    launchGameButton.querySelectorAll("span")[1].textContent = "启动游戏";
  } else if (!hasProfile) {
    launchGameButton.querySelectorAll("span")[1].textContent = "请先登录";
  } else if (!hasJava) {
    launchGameButton.querySelectorAll("span")[1].textContent =
      Number.isInteger(requiredJava) ? `需要 Java ${requiredJava}` : "需要 Java";
  } else {
    launchGameButton.querySelectorAll("span")[1].textContent = "启动游戏";
  }
}

function renderSystemState(state) {
  systemState = state;
  const installedJava = [state.java, ...(state.javaRuntimes || [])].filter(Boolean);
  javaStatus.textContent = installedJava.length
    ? `已准备 Java ${Math.max(...installedJava.map((java) => java.major))}`
    : "未检测到 Java，可在下载中心安装";
  javaStatus.classList.toggle("is-error", installedJava.length === 0);
  if (state.profile) {
    profileName.textContent = state.profile.name;
    profileStatus.textContent = "Minecraft Java 版";
    accountButton.textContent = "账号";
    logoutButton.hidden = false;
  } else {
    profileName.textContent = "未登录";
    profileStatus.textContent = state.clientIdConfigured ? "需要正版账号" : "先配置登录";
    accountButton.textContent = "登录";
    logoutButton.hidden = true;
  }
  const installed = state.installedVersions || [];
  installedVersionSelect.replaceChildren();
  installed.forEach((version) => {
    const option = document.createElement("option");
    option.value = version.id;
    option.textContent = version.id;
    installedVersionSelect.append(option);
  });
  installedVersionSelect.disabled = installed.length === 0;
  if (!installed.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "尚未完整安装";
    installedVersionSelect.append(option);
  }
  const statusPill = document.querySelector(".featured-meta .status-pill");
  const gameCardStatus = document.querySelector(".game-card-status");
  if (installed.length) {
    statusPill.innerHTML = '<span class="status-dot"></span> 已安装';
    gameCardStatus.innerHTML = '<span class="status-dot"></span> 已安装';
  } else {
    statusPill.innerHTML = '<span class="status-dot status-dot-muted"></span> 尚未安装';
    gameCardStatus.innerHTML = '<span class="status-dot status-dot-muted"></span> 等待安装';
  }
  updateJavaDownloadButton();
  updateLaunchAvailability();
}

async function refreshSystemState() {
  try {
    renderSystemState(await api("system"));
  } catch (error) {
    javaStatus.textContent = "请通过 python3 launcher.py 启动本机服务";
    javaStatus.classList.add("is-error");
    gameTaskState.hidden = false;
    gameTaskState.textContent = error.message;
    gameTaskState.classList.add("is-error");
  }
}

async function pollTask(taskId) {
  activeLaunchTaskId = taskId;
  updateLaunchAvailability();
  gameTaskState.hidden = false;
  gameTaskState.classList.remove("is-error");
  while (activeLaunchTaskId === taskId) {
    let task;
    try {
      task = await api(`tasks/${taskId}`);
    } catch (error) {
      gameTaskState.textContent = error.message;
      gameTaskState.classList.add("is-error");
      break;
    }
    let progress = "";
    if (task.totalBytes > 0) {
      progress = ` · ${Math.min(99, Math.floor(task.bytes / task.totalBytes * 100))}%`;
    } else if (task.total > 0) {
      progress = ` · ${task.completed}/${task.total}`;
    }
    gameTaskState.textContent = `${task.stage}${progress}${task.error ? `：${task.error}` : ""}`;
    if (task.status === "completed" || task.status === "failed" || task.status === "cancelled") {
      gameTaskState.classList.toggle("is-error", task.status !== "completed");
      if (task.status === "completed" && task.kind === "launch") {
        gameTaskState.textContent = `游戏进程已启动（PID ${task.processId}）。`;
      }
      if (task.status === "completed" && task.kind === "install") {
        await refreshSystemState();
      }
      activeLaunchTaskId = null;
      updateLaunchAvailability();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
  activeLaunchTaskId = null;
  updateLaunchAvailability();
}

function updateJavaDownloadButton() {
  const button = document.querySelector("#download-java");
  if (!button) return;
  const requiredJava = selectedVersion?.requiredJava;
  const installedJava = [systemState?.java, ...(systemState?.javaRuntimes || [])]
    .filter((java) => Number.isInteger(java?.major));
  const compatible = installedJava.find((java) =>
    !Number.isInteger(requiredJava) || java.major >= requiredJava,
  );
  button.disabled = downloadBusy || !selectedVersion?.id || !Number.isInteger(requiredJava) || Boolean(compatible);
  button.textContent = compatible
    ? `已准备 Java ${compatible.major}`
    : Number.isInteger(requiredJava)
      ? `下载 Java ${requiredJava}`
      : "该版本未提供 Java 要求";
}

function setDownloadBusy(busy) {
  downloadBusy = busy;
  versionType.disabled = busy;
  versionSearch.disabled = busy;
  versionSelect.disabled = busy || getVisibleVersions().length === 0;
  refreshVersionsButton.disabled = busy || manifestLoading;
  downloadClientButton.disabled = busy || !selectedVersion?.client;
  updateJavaDownloadButton();
  updateLaunchAvailability();
}

launchGameButton.addEventListener("click", async () => {
  if (!installedVersionSelect.value) {
    showView("downloads");
    return;
  }
  try {
    const result = await api("launch", {
      method: "POST",
      body: JSON.stringify({ version: installedVersionSelect.value }),
    });
    await pollTask(result.taskId);
  } catch (error) {
    gameTaskState.hidden = false;
    gameTaskState.textContent = error.message;
    gameTaskState.classList.add("is-error");
  }
});

installedVersionSelect.addEventListener("change", updateLaunchAvailability);

async function monitorDownloadTask(taskId, initialMessage) {
  activeInstallTaskId = taskId;
  setDownloadBusy(true);
  cancelDownloadButton.hidden = false;
  downloadProgress.hidden = false;
  downloadStatus.textContent = initialMessage;
  downloadProgressBar.removeAttribute("value");
  downloadPercent.textContent = "";
  while (activeInstallTaskId === taskId) {
    const task = await api(`tasks/${taskId}`);
    const completedFiles = task.total > 0 ? ` · ${task.completed}/${task.total} 个文件` : "";
    downloadStatus.textContent =
      `${task.stage}${completedFiles}${task.error ? `：${task.error}` : ""}`;
    if (task.totalBytes > 0) {
      const percentage = Math.min(99, Math.floor(task.bytes / task.totalBytes * 100));
      downloadProgressBar.value = percentage;
      downloadPercent.textContent = `${percentage}%`;
    }
    if (["completed", "failed", "cancelled"].includes(task.status)) {
      downloadProgressBar.value = task.status === "completed" ? 100 : 0;
      downloadPercent.textContent = task.status === "completed" ? "100%" : "";
      downloadStatus.textContent = task.error || task.stage;
      activeInstallTaskId = null;
      cancelDownloadButton.hidden = true;
      setDownloadBusy(false);
      await refreshSystemState();
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

document.querySelector("#download-client").addEventListener("click", async () => {
  if (!selectedVersion?.id) return;
  try {
    const result = await api("install", {
      method: "POST",
      body: JSON.stringify({ version: selectedVersion.id }),
    });
    await monitorDownloadTask(result.taskId, "正在完整安装游戏…");
  } catch (error) {
    activeInstallTaskId = null;
    downloadProgress.hidden = false;
    downloadStatus.textContent = error.message;
    cancelDownloadButton.hidden = true;
    setDownloadBusy(false);
  }
});

document.querySelector("#download-java").addEventListener("click", async () => {
  if (!selectedVersion?.id || !Number.isInteger(selectedVersion.requiredJava)) return;
  try {
    const result = await api("java/install", {
      method: "POST",
      body: JSON.stringify({ version: selectedVersion.id }),
    });
    await monitorDownloadTask(result.taskId, `正在下载 Mojang Java ${selectedVersion.requiredJava}…`);
  } catch (error) {
    activeInstallTaskId = null;
    downloadProgress.hidden = false;
    downloadStatus.textContent = error.message;
    cancelDownloadButton.hidden = true;
    setDownloadBusy(false);
  }
});

accountButton.addEventListener("click", () => {
  loginError.hidden = true;
  loginCodeBlock.hidden = true;
  loginDialog.showModal();
});
document.querySelector("#settings-login").addEventListener("click", () => {
  loginError.hidden = true;
  loginCodeBlock.hidden = true;
  loginDialog.showModal();
});

startLoginButton.addEventListener("click", async () => {
  loginError.hidden = true;
  startLoginButton.disabled = true;
  try {
    const flow = await api("auth/start", { method: "POST", body: "{}" });
    loginUserCode.textContent = flow.userCode;
    loginVerificationLink.href = flow.verificationUri || "https://microsoft.com/link";
    loginCodeBlock.hidden = false;
    loginPollStatus.textContent = "请完成 Microsoft 官方页面上的授权…";
    const interval = Math.max(3, flow.interval || 5) * 1000;
    const deadline = Date.now() + (flow.expiresIn || 900) * 1000;
    while (Date.now() < deadline && loginDialog.open) {
      await new Promise((resolve) => setTimeout(resolve, interval));
      if (!loginDialog.open) break;
      const result = await api("auth/poll", { method: "POST", body: "{}" });
      if (!result.pending) {
        loginPollStatus.textContent = `登录成功：${result.profile.name}`;
        await refreshSystemState();
        setTimeout(() => loginDialog.close(), 900);
        return;
      }
    }
  } catch (error) {
    loginError.textContent = error.message;
    loginError.hidden = false;
  } finally {
    startLoginButton.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  await fetch("/api/auth/profile", { method: "DELETE" });
  loginDialog.close();
  await refreshSystemState();
});

document.querySelector("#save-client-id").addEventListener("click", async () => {
  clientIdStatus.classList.remove("is-error");
  try {
    await api("config", {
      method: "POST",
      body: JSON.stringify({ clientId: clientIdInput.value.trim() }),
    });
    clientIdStatus.textContent = "Microsoft 登录配置已保存到本机。";
    await refreshSystemState();
  } catch (error) {
    clientIdStatus.textContent = error.message;
    clientIdStatus.classList.add("is-error");
  }
});

async function loadLauncherSettings() {
  const [configResult, settingsResult] = await Promise.allSettled([api("config"), api("settings")]);
  if (configResult.status === "fulfilled") {
    clientIdInput.value = configResult.value.clientId || "";
    clientIdStatus.textContent = "登录配置保存在本机。";
    clientIdStatus.classList.remove("is-error");
  } else {
    clientIdStatus.textContent = "启动本机服务后才可读取或保存登录配置。";
    clientIdStatus.classList.add("is-error");
  }
  if (settingsResult.status === "fulfilled") {
    if (!settingsDirty) {
      const settings = settingsResult.value;
      let browserCacheFailed = false;
      memoryInput.value = String(settings.memory ?? defaultSettings.memory);
      memoryValue.value = memoryInput.value;
      memoryValue.textContent = memoryInput.value;
      jvmArgumentsInput.value = settings.jvmArguments || "";
      try {
        localStorage.setItem(settingsKey, JSON.stringify({
          memory: Number(memoryInput.value),
          jvmArguments: jvmArgumentsInput.value,
        }));
      } catch {
        browserCacheFailed = true;
      }
      getSettingsFormValues();
      setSettingsStatus(
        browserCacheFailed
          ? "设置已从本机启动器读取；浏览器缓存不可用。"
          : "已加载本机保存的设置。",
      );
    }
  } else if (!settingsDirty) {
    setSettingsStatus("无法连接本机启动器；当前显示浏览器中上次保存的设置。", true);
  }
}

void refreshSystemState();
void loadLauncherSettings();

const versionType = document.querySelector("#version-type");
const versionSearch = document.querySelector("#version-search");
const versionSelect = document.querySelector("#version-select");
const refreshVersionsButton = document.querySelector("#refresh-versions");
const catalogState = document.querySelector("#catalog-state");
const versionDetail = document.querySelector("#version-detail");
const versionTypeBadge = document.querySelector("#version-type-badge");
const selectedVersionLabel = document.querySelector("#selected-version");
const releaseDateLabel = document.querySelector("#version-release-date");
const requiredJavaLabel = document.querySelector("#required-java");
const clientFileSizeLabel = document.querySelector("#client-file-size");
const downloadClientButton = document.querySelector("#download-client");
const cancelDownloadButton = document.querySelector("#cancel-download");
const downloadProgress = document.querySelector("#download-progress");
const downloadProgressBar = document.querySelector("#download-progress-bar");
const downloadStatus = document.querySelector("#download-status");
const downloadPercent = document.querySelector("#download-percent");
let availableVersions = [];
let selectedVersion = null;
let latestReleaseId = null;
let manifestRequest = null;
let selectionRequest = null;
let manifestLoaded = false;
let manifestLoading = false;

function setCatalogState(message, isError = false) {
  catalogState.replaceChildren();
  catalogState.classList.toggle("is-error", isError);
  if (!isError) {
    const spinner = document.createElement("span");
    spinner.className = "loading-spinner";
    spinner.setAttribute("aria-hidden", "true");
    catalogState.append(spinner);
  }
  const text = document.createElement("span");
  text.textContent = message;
  catalogState.append(text);
  catalogState.hidden = false;
}

function isOfficialUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" &&
      ["piston-meta.mojang.com", "piston-data.mojang.com", "launcher.mojang.com"].includes(url.hostname);
  } catch {
    return false;
  }
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "大小未知";
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getVisibleVersions() {
  const type = versionType.value;
  const query = versionSearch.value.trim().toLocaleLowerCase();
  return availableVersions.filter((version) => {
    const matchesType = type === "all" ||
      version.type === type ||
      (type === "all" && ["old_beta", "old_alpha"].includes(version.type));
    return matchesType && version.id.toLocaleLowerCase().includes(query);
  });
}

function renderVersionOptions(preferredId = null) {
  const versions = getVisibleVersions();
  versionSelect.replaceChildren();
  versions.forEach((version) => {
    const option = document.createElement("option");
    option.value = version.id;
    option.textContent = `${version.id}${version.id === latestReleaseId ? " · 最新正式版" : ""}`;
    versionSelect.append(option);
  });

  versionSelect.disabled = versions.length === 0;
  if (versions.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "没有匹配的版本";
    versionSelect.append(option);
    selectedVersion = null;
    versionDetail.hidden = true;
    return;
  }

  const selection = versions.find((version) => version.id === preferredId)
    ?? versions.find((version) => version.id === selectedVersion?.id)
    ?? versions[0];
  versionSelect.value = selection.id;
  if (selectedVersion?.id !== selection.id || !selectedVersion.client) {
    void loadVersionDetails(selection);
  }
}

async function loadVersionManifest() {
  manifestRequest?.abort();
  manifestRequest = new AbortController();
  const request = manifestRequest;
  manifestLoading = true;
  manifestLoaded = false;
  availableVersions = [];
  latestReleaseId = null;
  selectedVersion = null;
  refreshVersionsButton.disabled = true;
  versionSelect.disabled = true;
  versionDetail.hidden = true;
  setCatalogState("正在连接 Mojang 官方版本清单…");

  try {
    const result = await api("catalog/versions", { signal: request.signal });
    const manifest = result.manifest;
    if (request.signal.aborted) return;
    if (!manifest || !Array.isArray(manifest.versions) || !manifest.latest?.release) {
      throw new Error("官方版本清单格式无效。");
    }

    availableVersions = manifest.versions.filter((version) =>
      typeof version.id === "string" &&
      ["release", "snapshot", "old_beta", "old_alpha"].includes(version.type) &&
      isOfficialUrl(version.url) &&
      typeof version.releaseTime === "string",
    );
    latestReleaseId = manifest.latest.release;
    if (!availableVersions.some((version) => version.id === latestReleaseId)) {
      throw new Error("清单中未找到最新正式版。");
    }
    manifestLoaded = true;
    if (result.cached) {
      setCatalogState("网络暂不可用，正在显示上次缓存的官方版本清单。", true);
    } else {
      catalogState.hidden = true;
    }
    renderVersionOptions(latestReleaseId);
  } catch (error) {
    if (request.signal.aborted) return;
    const message = error instanceof Error ? error.message : "读取官方版本清单时发生未知错误。";
    versionSelect.replaceChildren();
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "加载失败，请点击刷新重试";
    versionSelect.append(option);
    setCatalogState(`${message} 请检查网络后重试。`, true);
  } finally {
    if (manifestRequest === request) {
      manifestLoading = false;
      refreshVersionsButton.disabled = false;
      if (availableVersions.length > 0) versionSelect.disabled = getVisibleVersions().length === 0;
    }
  }
}

async function loadVersionDetails(version) {
  selectionRequest?.abort();
  selectionRequest = new AbortController();
  const request = selectionRequest;
  selectedVersion = version;
  updateJavaDownloadButton();
  versionDetail.hidden = false;
  downloadClientButton.disabled = true;
  downloadProgress.hidden = true;
  selectedVersionLabel.textContent = version.id;
  versionTypeBadge.textContent = version.type === "release"
    ? "正式版"
    : version.type === "snapshot"
      ? "快照版"
      : "旧版";
  releaseDateLabel.textContent = `发布日期：${version.releaseTime.slice(0, 10)}`;
  clientFileSizeLabel.textContent = "读取客户端信息…";

  try {
    const result = await api(`catalog/versions/${encodeURIComponent(version.id)}`, {
      signal: request.signal,
    });
    const details = result.metadata;
    if (request.signal.aborted) return;
    const client = details?.downloads?.client;
    if (
      !client ||
      !isOfficialUrl(client.url) ||
      typeof client.sha1 !== "string" ||
      !/^[a-f0-9]{40}$/i.test(client.sha1) ||
      !Number.isSafeInteger(client.size) ||
      client.size <= 0
    ) {
      throw new Error("该版本的官方客户端信息缺失或格式无效。");
    }
    selectedVersion = { ...version, client };
    selectedVersion.requiredJava = details.javaVersion?.majorVersion;
    selectedVersion.javaComponent = details.javaVersion?.component;
    clientFileSizeLabel.textContent = `客户端核心 ${formatFileSize(client.size)}`;
    requiredJavaLabel.textContent = selectedVersion.requiredJava
      ? `需要 Java ${selectedVersion.requiredJava} 或更高版本`
      : "";
    if (result.cached) {
      setCatalogState("网络暂不可用，正在显示已缓存的官方版本信息。", true);
    } else if (manifestLoaded && catalogState.textContent.includes("上次缓存")) {
      catalogState.hidden = true;
    }
    downloadClientButton.disabled = downloadBusy ? true : false;
    updateJavaDownloadButton();
  } catch (error) {
    if (request.signal.aborted) return;
    const message = error instanceof Error ? error.message : "读取客户端信息时发生未知错误。";
    clientFileSizeLabel.textContent = message;
    downloadClientButton.disabled = true;
    updateJavaDownloadButton();
  }
}

versionType.addEventListener("change", () => renderVersionOptions());
versionSearch.addEventListener("input", () => renderVersionOptions());
versionSelect.addEventListener("change", () => {
  const version = availableVersions.find((item) => item.id === versionSelect.value);
  if (version) void loadVersionDetails(version);
});
refreshVersionsButton.addEventListener("click", () => void loadVersionManifest());
cancelDownloadButton.addEventListener("click", async () => {
  if (!activeInstallTaskId) return;
  try {
    const response = await fetch(`/api/tasks/${activeInstallTaskId}`, { method: "DELETE" });
    if (!response.ok) throw new Error("取消下载请求失败。");
    downloadStatus.textContent = "正在取消下载…";
  } catch (error) {
    downloadStatus.textContent = error.message;
  }
});
