/** Shared clipboard helpers for chat + artifact panes. */

export async function copyText(text: string): Promise<void> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      /* fall through */
    }
  }
  fallbackCopy(text);
}

function fallbackCopy(text: string): void {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.left = "-9999px";
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  document.body.removeChild(ta);
}

/** Format labeled rows as plain text for clipboard. */
export function formatCopyBlock(
  title: string,
  rows: Array<{ label: string; value: string | number | null | undefined }>,
): string {
  const lines = rows
    .filter((r) => r.value !== null && r.value !== undefined && r.value !== "")
    .map((r) => `${r.label}: ${r.value}`);
  return [title, ...lines].join("\n");
}

export function formatCopyLines(title: string, lines: string[]): string {
  const body = lines.filter(Boolean);
  return [title, ...body].join("\n");
}
