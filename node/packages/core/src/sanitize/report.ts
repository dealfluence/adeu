export class SanitizeReport {
  public filename: string;
  public mode: string;
  public author: string | null;

  public tracked_changes_found: number = 0;
  public tracked_changes_accepted: number = 0;
  public tracked_changes_kept: number = 0;
  public change_lines: string[] = [];

  public comments_removed: number = 0;
  public comments_kept: number = 0;
  public removed_comment_lines: string[] = [];
  public kept_comment_lines: string[] = [];

  public metadata_lines: string[] = [];
  public structural_lines: string[] = [];
  public warnings: string[] = [];

  public status: string = "clean";
  public blocked_reason: string | null = null;

  constructor(filename: string, mode: string = "full", author: string | null = null) {
    this.filename = filename;
    this.mode = mode;
    this.author = author;
  }

  public add_transform_lines(lines: string[]) {
    for (const line of lines) {
      const lower = line.toLowerCase();
      if (lower.includes("tracked change") || lower.includes("insertion") || lower.includes("deletion") || lower.includes("accepted")) {
        this.change_lines.push(line);
      } else if (
        lower.includes("author") || lower.includes("template") || lower.includes("company") ||
        lower.includes("manager") || lower.includes("metadata") || lower.includes("timestamp") ||
        lower.includes("custom xml") || lower.includes("custom propert") || lower.includes("identifier") ||
        lower.includes("document variable") ||
        lower.includes("language") || lower.includes("version") ||
        lower.includes("last modified by") || lower.includes("revision count") || lower.includes("last printed") ||
        lower.includes("description/comments")
      ) {
        this.metadata_lines.push(line);
      } else if (lower.includes("comment") || lower.includes("[open]") || lower.includes("[resolved]")) {
        if (lower.includes("kept") || lower.includes("visible")) {
          this.kept_comment_lines.push(line);
        } else {
          this.removed_comment_lines.push(line);
        }
      } else if (lower.includes("hyperlink") || lower.includes("warning")) {
        this.warnings.push(line);
      } else {
        this.structural_lines.push(line);
      }
    }
  }

  public render(): string {
    const sep = "═".repeat(50);
    const lines: string[] = [sep, `Finalization Report: ${this.filename}`];

    const flags: string[] = [];
    if (this.mode === "keep-markup") flags.push("--keep-markup");
    if (this.author) flags.push(`--author "${this.author}"`);
    if (this.tracked_changes_accepted > 0) flags.push("--accept-all");

    if (flags.length > 0) lines.push(flags.join(" "));
    lines.push(sep);

    if (this.status === "blocked") {
      lines.push("");
      lines.push(`BLOCKED: ${this.blocked_reason}`);
      lines.push(sep);
      return lines.join("\n");
    }

    if (this.mode === "keep-markup" && (this.tracked_changes_kept > 0 || this.comments_kept > 0)) {
      lines.push("");
      lines.push("VISIBLE TO COUNTERPARTY");
      if (this.tracked_changes_kept > 0) lines.push(`  Tracked changes: ${this.tracked_changes_kept}`);
      if (this.comments_kept > 0) {
        lines.push(`  Open comments: ${this.comments_kept}`);
        for (const cl of this.kept_comment_lines) lines.push(`    ${cl}`);
      }
      if (this.author) lines.push(`  Author on all markup: "${this.author}"`);
    }

    if (this.change_lines.length > 0) {
      lines.push("");
      lines.push("TRACKED CHANGES");
      for (const cl of this.change_lines) lines.push(`  ${cl}`);
    }

    if (this.removed_comment_lines.length > 0) {
      lines.push("");
      lines.push("COMMENTS (stripped)");
      for (const cl of this.removed_comment_lines) lines.push(`  ${cl}`);
    }

    if (this.metadata_lines.length > 0) {
      lines.push("");
      lines.push("METADATA");
      for (const ml of this.metadata_lines) lines.push(`  ${ml}`);
    }

    if (this.structural_lines.length > 0) {
      lines.push("");
      lines.push("STRUCTURAL & PROTECTION");
      for (const sl of this.structural_lines) lines.push(`  ${sl}`);
    }

    if (this.warnings.length > 0) {
      lines.push("");
      lines.push("WARNINGS");
      for (const w of this.warnings) lines.push(`  ⚠ ${w}`);
    }

    lines.push("");
    lines.push(sep);
    if (this.warnings.length > 0) {
      lines.push(`Result: CLEAN WITH WARNINGS (${this.warnings.length} warning${this.warnings.length > 1 ? 's' : ''})`);
    } else {
      lines.push(`Result: CLEAN (${this.tracked_changes_found} changes resolved, ${this.comments_removed} comments removed)`);
    }
    lines.push(sep);

    return lines.join("\n");
  }
}