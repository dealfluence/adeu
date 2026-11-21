import fs from "node:fs";
import { randomUUID } from "node:crypto";
import { dirname, extname, join, parse, resolve } from "node:path";
import { fromFdPromise } from "yauzl";

const openEndings = [".docx", ".docm", ".dotx", ".dotm"];
const wordEndings = [...openEndings, ".doc", ".dot"];
const restrictedEndings = [".docm", ".dotm", ".dotx", ".dot"];
const windows = process.platform === "win32";

class PathRefusal extends Error {}

function refuse(path: string, reason: string, advice: string): never {
  throw new PathRefusal(`Refused '${path}': ${reason}. ${advice}.`);
}

function ordinaryPath(path: string): string {
  if (!path || path.includes("\0"))
    refuse(path, "invalid file path", "Supply a filename");
  if (!windows) return path;
  // Permit ordinary drive and UNC paths in their extended-length spelling.
  const name = path
    .replaceAll("/", "\\")
    .replace(/^\\\\\?\\UNC\\/i, "\\\\")
    .replace(/^\\\\\?\\(?=[a-z]:\\)/i, "");
  const reserved =
    /^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]) *(?:\.|$)/i;
  const components = name.replace(/^[a-z]:/i, "").split("\\");
  if (
    /^(?:\\\\[?.]\\|\\\?\?\\)/.test(name) ||
    /^\\\\[^\\]+\\(?:PIPE|MAILSLOT)(?:\\|$)/i.test(name) ||
    name.replace(/^[a-z]:/i, "").includes(":") ||
    components.some(
      (part) =>
        part !== "." &&
        part !== ".." &&
        (/[. ]$/.test(part) || reserved.test(part)),
    )
  ) {
    refuse(
      path,
      "Windows treats this as a device or ambiguous filename",
      "Use an ordinary filename without extra colons or trailing dots or spaces",
    );
  }
  return name;
}

function sameFolder(first: string, second: string): boolean {
  const a = dirname(first);
  const b = dirname(second);
  if (a === b) return true;
  if (!windows) return false;
  // Windows also supports case-sensitive directories. Different spellings
  // qualify only when both parents' filesystem identities agree.
  const left = fs.statSync(a, { bigint: true, throwIfNoEntry: false });
  const right = fs.statSync(b, { bigint: true, throwIfNoEntry: false });
  return (
    !!left &&
    !!right &&
    left.ino !== 0n &&
    left.ino === right.ino &&
    left.dev === right.dev
  );
}

/** Walk in filesystem order so a link followed by '..' uses its real parent.
 * Missing components stay in the result, including the referent of a dangling link. */
function canonicalPath(path: string, base = process.cwd(), links = 0): string {
  path = ordinaryPath(path);
  if (links > 40) refuse(path, "too many symbolic links", "Use a direct path");
  const root = parse(path).root;
  let current = root ? resolve(base, root) : base;
  for (const part of path.slice(root.length).split(windows ? /[\\/]/ : "/")) {
    if (!part || part === ".") continue;
    const parent = fs.lstatSync(current, { throwIfNoEntry: false });
    if (parent && !parent.isDirectory())
      refuse(path, "invalid parent folder", "Choose an accessible folder");
    if (part === "..") {
      current = dirname(current);
      continue;
    }
    const child = join(current, part);
    const stat = fs.lstatSync(child, { throwIfNoEntry: false });
    if (stat?.isSymbolicLink()) {
      current = canonicalPath(fs.readlinkSync(child), current, links + 1);
    } else {
      // Also expand Windows short names and junction ancestors.
      current =
        stat?.isFile() || stat?.isDirectory()
          ? fs.realpathSync.native(child)
          : child;
    }
  }
  return ordinaryPath(current);
}

async function packageKind(
  path: string,
): Promise<"word" | "other" | "invalid"> {
  const { O_RDONLY, O_NONBLOCK, O_NOFOLLOW } = fs.constants;
  const fd = fs.openSync(
    path,
    O_RDONLY | (O_NONBLOCK ?? 0) | (O_NOFOLLOW ?? 0),
  );
  try {
    if (!fs.fstatSync(fd).isFile())
      refuse(path, "not a regular file", "Use a regular Word document");
    const archive = await fromFdPromise(fd, {
      autoClose: false,
      decodeStrings: false,
      validateEntrySizes: false,
    });
    let types = false;
    let main = false;
    for await (const entry of archive.eachEntry()) {
      const name = entry.fileNameRaw.toString("latin1");
      if (name === "[Content_Types].xml") types = true;
      if (name === "word/document.xml") main = true;
    }
    return types && main ? "word" : "other";
  } catch (err) {
    // I/O failure is different from an interrupted, malformed package.
    if (err instanceof PathRefusal || (err as NodeJS.ErrnoException).code)
      throw err;
    return "invalid";
  } finally {
    fs.closeSync(fd);
  }
}

type PathUse = "source" | "open" | "write" | "pdf";

export async function checkToolPath(
  path: string,
  use: PathUse,
  inputs: string[] = [],
): Promise<string> {
  try {
    const target = canonicalPath(path);
    const stat = fs.lstatSync(target, { throwIfNoEntry: false });
    if (use === "source") {
      // Preserve the reader's missing-file error, including the original spelling.
      return stat ? target : path;
    }
    const ext = extname(target).toLowerCase();
    if (stat && !stat.isFile())
      refuse(
        path,
        "not a regular file",
        "Use a regular Word document or a new output filename",
      );
    if (use === "open") {
      if (
        !stat ||
        !openEndings.includes(ext) ||
        (await packageKind(target)) !== "word"
      ) {
        refuse(
          path,
          "opening requires an existing Word package",
          "Choose a regular .docx, .docm, .dotx or .dotm Word document",
        );
      }
      return target;
    }
    const sources = inputs.map((input) => canonicalPath(input));
    const matching = sources.filter(
      (source) => extname(source).toLowerCase() === ext,
    );
    if (
      use === "pdf"
        ? ext !== ".pdf"
        : !ext || ext === ".pdf" || (ext !== ".docx" && !matching.length)
    ) {
      refuse(
        path,
        "the output ending is not permitted",
        use === "pdf"
          ? "Use .pdf for the PDF output"
          : "Use .docx or the input document's ending",
      );
    }
    if (
      restrictedEndings.includes(ext) &&
      !matching.some((source) => sameFolder(source, target))
    ) {
      refuse(
        path,
        "this ending is restricted to the input document's folder",
        "Use .docx here or save beside the input with that ending",
      );
    }
    if (!stat || !stat.size) return target;
    const kind = await packageKind(target);
    if (
      kind === "word" ||
      (kind === "invalid" &&
        wordEndings.includes(ext) &&
        sources.some((source) => sameFolder(source, target)))
    )
      return target;
    refuse(
      path,
      "the existing file is not a Word package",
      "Choose a new filename or an empty regular file",
    );
  } catch (err) {
    if (err instanceof PathRefusal) throw err;
    refuse(
      path,
      `the path could not be checked (${(err as NodeJS.ErrnoException).code ?? "filesystem error"})`,
      "Use an accessible regular file or a new output filename",
    );
  }
}

/** Explicit tool parameters are checked by the registration wrapper. Derived
 * names and current destination contents are checked again at the write. */
const toolPaths: Record<string, { inputs: string[]; output?: string }> = {
  read_docx: { inputs: ["file_path"] },
  process_document_batch: {
    inputs: ["original_docx_path"],
    output: "output_path",
  },
  accept_all_changes: { inputs: ["docx_path"], output: "output_path" },
  diff_docx_files: {
    inputs: ["original_path", "modified_path"],
    output: "output_path",
  },
  finalize_document: { inputs: ["file_path"], output: "output_path" },
  apply_text_revision: { inputs: ["file_path"], output: "output_path" },
};

export async function checkToolArguments(
  tool: string,
  args: Record<string, unknown>,
) {
  const policy = toolPaths[tool];
  if (!policy) return args;
  const checked = { ...args };
  const sources: string[] = [];
  for (const key of policy.inputs) {
    const source = await checkToolPath(args[key] as string, "source");
    checked[key] = source;
    sources.push(source);
  }
  if (policy.output && args[policy.output] !== undefined) {
    checked[policy.output] = await checkToolPath(
      args[policy.output] as string,
      "write",
      sources,
    );
  }
  return checked;
}

export async function writeToolDocument(
  path: string,
  data: Uint8Array,
  inputs: string[],
) {
  const target = await checkToolPath(path, "write", inputs);
  const existing = fs.lstatSync(target, { throwIfNoEntry: false });
  const parent = dirname(target);
  fs.mkdirSync(parent, { recursive: true });
  // A new file is claimed exclusively. Replace an existing directory entry
  // atomically so hard links to it cannot overwrite another file's contents.
  if (!existing) {
    fs.writeFileSync(target, data, { flag: "wx" });
  } else {
    const temporary = join(parent, `.adeu-${randomUUID()}.docx`);
    const fd = fs.openSync(temporary, "wx", existing.mode & 0o777);
    try {
      try {
        fs.writeFileSync(fd, data);
      } finally {
        fs.closeSync(fd);
      }
      fs.renameSync(temporary, target);
    } finally {
      fs.rmSync(temporary, { force: true });
    }
  }
  return { path: target, existedBefore: !!existing };
}
