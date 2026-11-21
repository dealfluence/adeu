import fs from "node:fs";
import { randomUUID } from "node:crypto";
import {
  basename,
  delimiter,
  dirname,
  extname,
  isAbsolute,
  join,
  parse,
  resolve,
} from "node:path";
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
 * Tool paths can be new. Configured roots must exist at every step. */
function canonicalPath(
  path: string,
  strict = false,
  base = process.cwd(),
  links = 0,
): string {
  path = ordinaryPath(path);
  if (links > 40) refuse(path, "too many symbolic links", "Use a direct path");
  const root = parse(path).root;
  let current = root ? resolve(base, root) : base;
  for (const part of path.slice(root.length).split(windows ? /[\\/]/ : "/")) {
    if (!part || part === ".") continue;
    const parent = fs.lstatSync(current, { throwIfNoEntry: strict });
    if (parent && !parent.isDirectory()) {
      if (strict)
        throw Object.assign(new Error("Invalid parent folder"), {
          code: "ENOTDIR",
        });
      refuse(path, "invalid parent folder", "Choose an accessible folder");
    }
    if (part === "..") {
      current = dirname(current);
      continue;
    }
    const child = join(current, part);
    const stat = fs.lstatSync(child, { throwIfNoEntry: strict });
    if (stat?.isSymbolicLink()) {
      current = canonicalPath(
        fs.readlinkSync(child),
        strict,
        current,
        links + 1,
      );
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

/** Capture directory identities at startup. Aliases, including short names
 * and case/Unicode variants, are resolved by the host filesystem. */
function readAllowedFolders(value: string) {
  const entries = value.split(delimiter).filter((entry) => entry.trim());
  if (!entries.length)
    throw new Error("ADEU_ALLOWED_DIRS is set but contains no folders.");
  const roots = entries.map((entry) => {
    const prefix = `ADEU_ALLOWED_DIRS entry '${entry}'`;
    if (!isAbsolute(entry) || (windows && parse(entry).root.length === 1))
      throw new Error(`${prefix} is relative. Use an absolute folder path.`);
    let path: string;
    let stat: fs.BigIntStats;
    try {
      path = canonicalPath(entry, true);
      stat = fs.statSync(path, { bigint: true });
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === "ENOENT") throw new Error(`${prefix} does not exist.`);
      if (code === "ENOTDIR") throw new Error(`${prefix} is not a folder.`);
      throw new Error(`${prefix} cannot be resolved.`);
    }
    if (!stat.isDirectory()) throw new Error(`${prefix} is not a folder.`);
    return { path, ino: stat.ino, dev: stat.dev };
  });
  const listing = roots.map((root) => root.path).join(delimiter);
  return {
    listing,
    resolve(path: string): string {
      try {
        const target = canonicalPath(path);
        for (let dir = target; ; dir = dirname(dir)) {
          const stat = fs.statSync(dir, {
            bigint: true,
            throwIfNoEntry: false,
          });
          if (
            stat?.isDirectory() &&
            roots.some((root) =>
              root.ino
                ? root.ino === stat.ino && root.dev === stat.dev
                : stat.ino === 0n && root.path === dir,
            )
          )
            return target;
          if (dirname(dir) === dir) break;
        }
      } catch {
        // An inaccessible or unresolvable candidate gets the same refusal.
      }
      throw new PathRefusal(`File access refused. Allowed folders: ${listing}`);
    },
  };
}

// This module belongs to the server, which loads it once before accepting
// requests. Tool calls cannot change this snapshot.
const allowedFolders = (() => {
  const value = process.env.ADEU_ALLOWED_DIRS;
  if (value === undefined) return undefined;
  try {
    const folders = readAllowedFolders(value);
    console.error(`ADEU_ALLOWED_DIRS allowed folders: ${folders.listing}`);
    return folders;
  } catch (error) {
    console.error((error as Error).message);
    process.exit(1);
  }
})();

export function listDocumentSiblings(dir: string): string[] {
  const folder = allowedFolders ? allowedFolders.resolve(dir) : dir;
  const names = fs
    .readdirSync(folder)
    .filter((name) => name.toLowerCase().endsWith(".docx"));
  if (!allowedFolders) return names;
  // A name in an allowed folder may itself point outside. Count and rank
  // only names that the caller would be allowed to use.
  return names.filter((name) => {
    try {
      allowedFolders.resolve(join(folder, name));
      return true;
    } catch {
      return false;
    }
  });
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
    const target = allowedFolders
      ? allowedFolders.resolve(path)
      : canonicalPath(path);
    const stat = fs.lstatSync(target, { throwIfNoEntry: false });
    if (use === "source") {
      // An unconfined missing-file error retains the caller's spelling.
      // Confined reads must also use the checked parent for filename hints.
      return stat || allowedFolders ? target : path;
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
    const sources = inputs.map((input) =>
      allowedFolders ? allowedFolders.resolve(input) : canonicalPath(input),
    );
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

type DefaultOutput = "processed" | "clean" | "final" | "redlined";

export function defaultOutputPath(
  source: string,
  suffix: DefaultOutput,
): string {
  if (suffix === "redlined") {
    // Match apply_text_revision's core naming, including dotfiles.
    const name = basename(source);
    const dot = name.lastIndexOf(".");
    const stem = dot === -1 ? name : name.slice(0, dot);
    return stem.endsWith("_redlined") || stem.endsWith("_processed")
      ? source
      : join(dirname(source), `${stem}_redlined.docx`);
  }
  const ext = extname(source);
  const stem = basename(source, ext);
  if (
    suffix === "processed" &&
    /(?:_|-|\.)(processed|redlined|redline|markup|marked)$/i.test(stem)
  )
    return resolve(dirname(source), `${stem}${ext}`);
  return resolve(dirname(source), `${stem}_${suffix}${ext}`);
}

export function diagnosticPath(output: string): string {
  return join(
    dirname(output),
    `${basename(output, extname(output))}.unverified.docx`,
  );
}

/** The registration wrapper checks all arguments and confined derived names
 * before invoking a handler. Writers check current destination contents again. */
const toolPaths: Record<
  string,
  { inputs: string[]; output?: string; defaultOutput?: DefaultOutput }
> = {
  read_docx: { inputs: ["file_path"] },
  process_document_batch: {
    inputs: ["original_docx_path"],
    output: "output_path",
    defaultOutput: "processed",
  },
  accept_all_changes: {
    inputs: ["docx_path"],
    output: "output_path",
    defaultOutput: "clean",
  },
  diff_docx_files: {
    inputs: ["original_path", "modified_path"],
    output: "output_path",
  },
  finalize_document: {
    inputs: ["file_path"],
    output: "output_path",
    defaultOutput: "final",
  },
  apply_text_revision: {
    inputs: ["file_path"],
    output: "output_path",
    defaultOutput: "redlined",
  },
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
  if (policy.output) {
    let output = args[policy.output] as string | undefined;
    if (allowedFolders) {
      if (output === undefined && policy.defaultOutput)
        output = defaultOutputPath(sources[0], policy.defaultOutput);
      if (output !== undefined) {
        output = allowedFolders.resolve(output);
        // Preflight both outcomes before checking any ZIP contents.
        if (policy.defaultOutput === "redlined")
          allowedFolders.resolve(diagnosticPath(output));
      }
    }
    if (output !== undefined)
      checked[policy.output] = await checkToolPath(output, "write", sources);
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
