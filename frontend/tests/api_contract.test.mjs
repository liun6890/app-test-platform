import test from "node:test";
import assert from "node:assert/strict";

import { ApiError, api } from "../src/api.ts";

test("rejects a successful JSON null response instead of returning null", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("null", {
    status: 200,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

  try {
    await assert.rejects(
      api.devices(),
      (error) => error instanceof ApiError
        && error.status === 200
        && error.message === "本地服务返回了空数据",
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test("rejects an HTML fallback response with an actionable service error", async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("<!doctype html><html></html>", {
    status: 200,
    headers: { "content-type": "text/html" },
  });

  try {
    await assert.rejects(
      api.devices(),
      (error) => error instanceof ApiError
        && error.status === 200
        && error.message === "本地接口返回了网页，请重启本地控制台服务后重试",
    );
  } finally {
    globalThis.fetch = previousFetch;
  }
});
