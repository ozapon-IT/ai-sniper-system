import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest の globals を有効にしていないため、RTL の自動 cleanup が効かない。明示的に呼ぶ。
afterEach(() => cleanup());
