import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const page = readFileSync(new URL("../recall_media.html", import.meta.url), "utf8");
const inlineScript = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)?.[1];

test("Recall meeting capture requests unprocessed meeting audio", async () => {
  assert.ok(inlineScript, "Recall media page includes its client script");

  const status = { dataset: {}, textContent: "" };
  const captureRequests = [];
  const clientMessages = [];

  class FakeAudioContext {
    constructor({ sampleRate }) {
      this.sampleRate = sampleRate;
      this.state = "running";
      this.currentTime = 0;
      this.destination = {};
      this.audioWorklet = { addModule: async () => {} };
    }

    createMediaStreamSource() {
      return { connect: (next) => next, disconnect() {} };
    }

    createGain() {
      return { gain: { value: 1 }, connect: (next) => next, disconnect() {} };
    }

    async close() {
      this.state = "closed";
    }
  }

  class FakeAudioWorkletNode {
    constructor() {
      this.port = {};
    }

    connect(next) {
      return next;
    }

    disconnect() {}
  }

  class FakeWebSocket {
    static OPEN = 1;

    constructor() {
      this.readyState = 0;
      queueMicrotask(() => {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen?.();
        this.onmessage?.({ data: JSON.stringify({ type: "ready", interactionMode: "copilot" }) });
      });
    }

    send(data) {
      if (typeof data === "string") clientMessages.push(JSON.parse(data));
    }

    close() {
      this.readyState = 3;
    }
  }

  const context = {
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeAudioWorkletNode,
    Blob,
    URL: { createObjectURL: () => "blob:worklet", revokeObjectURL() {} },
    URLSearchParams,
    WebSocket: FakeWebSocket,
    clearTimeout() {},
    document: { getElementById: () => status },
    history: { replaceState() {} },
    location: { hash: "#session=test-ticket", host: "voice.test", pathname: "/recall/media", protocol: "https:" },
    navigator: {
      mediaDevices: {
        async getUserMedia(options) {
          captureRequests.push(options);
          return { getTracks: () => [{ stop() {} }] };
        },
      },
    },
    setTimeout: () => 1,
    window: { addEventListener() {} },
  };

  vm.runInNewContext(inlineScript, context);
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(captureRequests.length, 1, "the page starts one meeting-audio capture");
  assert.deepEqual(
    JSON.parse(JSON.stringify(captureRequests[0])),
    { audio: { autoGainControl: false, echoCancellation: false, noiseSuppression: false } },
    "the virtual meeting feed is not modified by microphone-oriented browser processing",
  );
  assert.ok(clientMessages.some((message) => message.type === "ready" && message.sampleRate === 48000));
});
