import { strFromU8, unzipSync } from 'fflate';
import { DocumentObject } from '../docx/bridge.js';
import { SanitizeReport } from './report.js';
import * as transforms from './transforms.js';
import { findAllDescendants, parseXml } from '../docx/dom.js';

export interface FinalizeOptions {
  filename: string;
  sanitize_mode?: 'full' | 'keep-markup' | 'baseline';
  accept_all?: boolean;
  protection_mode?: 'read_only' | 'encrypt' | null;
  password?: string | null;
  author?: string | null;
  export_pdf?: boolean;
}

export interface FinalizeResult {
  reportText: string;
  outBuffer?: Buffer;
}

export async function finalize_document(doc: DocumentObject, options: FinalizeOptions): Promise<FinalizeResult> {
  const report = new SanitizeReport(options.filename, options.sanitize_mode || 'full', options.author || null);

  if (options.sanitize_mode === 'full') {
    const counts = transforms.count_tracked_changes(doc);
    const total = counts[0] + counts[1] + counts[2];
    report.tracked_changes_found = total;

    if (total > 0 && !options.accept_all) {
      report.status = 'blocked';
      report.blocked_reason = `Document contains ${total} unresolved tracked changes (${counts[0]} insertions, ${counts[1]} deletions, ${counts[2]} formatting). Review in Word first, or set accept_all=true.`;
      return { reportText: report.render() };
    }

    if (total > 0) {
      const authors = transforms.get_track_change_authors(doc);
      if (authors.size > 1) {
        report.warnings.push(`Multiple authors detected in tracked changes: ${Array.from(authors).sort().join(', ')}. Review per-change list before sending.`);
      }
      report.add_transform_lines(transforms.accept_all_tracked_changes(doc));
      report.tracked_changes_accepted = total;
    }

    const commentsSummary = transforms.get_comments_summary(doc);
    report.comments_removed = commentsSummary.total;
    report.add_transform_lines(transforms.remove_all_comments(doc));
    transforms.eject_comment_parts(doc);
  } else if (options.sanitize_mode === 'keep-markup') {
    // Basic support for keep-markup in TS
    const counts = transforms.count_tracked_changes(doc);
    report.tracked_changes_found = counts[0] + counts[1] + counts[2];
    report.tracked_changes_kept = report.tracked_changes_found;

    if (options.author) {
      report.add_transform_lines(transforms.replace_comment_authors(doc, options.author));
      report.add_transform_lines(transforms.replace_change_authors(doc, options.author));
    }
  }

  // Common transforms
  report.add_transform_lines(transforms.strip_rsid(doc));
  report.add_transform_lines(transforms.strip_para_ids(doc));
  report.add_transform_lines(transforms.strip_proof_errors(doc));
  report.add_transform_lines(transforms.strip_empty_properties(doc));
  report.add_transform_lines(transforms.strip_hidden_text(doc));
  report.add_transform_lines(transforms.coalesce_runs(doc));
  report.add_transform_lines(transforms.scrub_doc_properties(doc));
  report.add_transform_lines(transforms.scrub_timestamps(doc));
  report.add_transform_lines(transforms.strip_custom_xml(doc));
  report.add_transform_lines(transforms.strip_custom_properties(doc));
  report.add_transform_lines(transforms.strip_document_variables(doc));
  report.add_transform_lines(transforms.strip_image_alt_text(doc));
  
  const warnings = transforms.audit_hyperlinks(doc);
  for (const w of warnings) report.warnings.push(w);
  // Audit (non-destructive — just warnings): watermark-like VML text objects
  // must never let the report declare the document clean (QA 2026-07-18 M3).
  for (const w of transforms.detect_watermarks(doc)) report.warnings.push(w);

  report.add_transform_lines(transforms.normalize_change_dates(doc));
  // Retained comments carry the same when-did-they-work signal in
  // word/comments.xml as tracked changes do in the body (QA 2026-07-19 F-09).
  report.add_transform_lines(transforms.normalize_comment_dates(doc));

  // Protection (Settings injection)
  if (options.protection_mode === 'read_only' || options.protection_mode === 'encrypt') {
    if (options.protection_mode === 'encrypt') {
      report.warnings.push("Encryption mode (AES compound wrappers) is strictly unsupported in the zero-dependency Node engine. Falling back to native Word Read-Only lock.");
    }
    
    const settingsPart = doc.pkg.getPartByPath('word/settings.xml');
    if (settingsPart) {
      const docEl = settingsPart._element.ownerDocument!;
      let prot = transforms.findDescendantsByLocalName(settingsPart._element, 'documentProtection')[0];
      if (!prot) {
        prot = docEl.createElement('w:documentProtection');
        // Word expects documentProtection to be inserted before elements like w:autoFormatOverride, w:styleLockTheme, etc.
        // For standard robustness without complex XSD enforcement, appendChild generally works.
        settingsPart._element.appendChild(prot);
      }
      prot.setAttribute('w:edit', 'readOnly');
      prot.setAttribute('w:enforcement', '1');
      report.structural_lines.push("Document locked (Read-Only enforcement injected into settings.xml)");
    }
  }

  if (options.export_pdf) {
    report.warnings.push("PDF export requires the Python/Word COM environment and is skipped in this zero-dependency Node agent.");
  }

  // Clean up leaked Microsoft namespaces
  for (const part of doc.pkg.parts) {
    // Match the exact injection condition from RedlineEngine constructor
    if (part === doc.part || (part.contentType.includes('wordprocessingml') && part.contentType.endsWith('+xml'))) {
      if (part._element.hasAttribute('xmlns:w16du')) {
        let hasW16du = false;
        // Check root element attributes (excluding the xmlns declaration itself)
        if (Array.from(part._element.attributes || []).some(a => a.name.startsWith('w16du:') && a.name !== 'xmlns:w16du')) {
          hasW16du = true;
        }
        if (!hasW16du) {
          const allNodes = findAllDescendants(part._element, '*');
          for (const n of allNodes) {
            if (n.tagName.startsWith('w16du:') || Array.from(n.attributes || []).some(a => a.name.startsWith('w16du:'))) {
              hasW16du = true;
              break;
            }
          }
        }
        if (!hasW16du) part._element.removeAttribute('xmlns:w16du');
      }
    }
  }

  if (report.warnings.length > 0) report.status = 'clean_with_warnings';

  const outBuffer = await doc.save();
  verifySanitizedPackage(outBuffer);
  return { reportText: report.render(), outBuffer };
}

// Core-property elements the pipeline claims to scrub; the post-sanitize
// verification re-checks each one in the SAVED bytes.
const VERIFIED_CORE_FIELDS: Array<[string, string]> = [
  ['creator', 'author (dc:creator)'],
  ['lastModifiedBy', 'last modified by (cp:lastModifiedBy)'],
  ['identifier', 'identifier (dc:identifier)'],
  ['description', 'description (dc:description)'],
  ['keywords', 'keywords (cp:keywords)'],
  ['category', 'category (cp:category)'],
  ['subject', 'subject (dc:subject)'],
  ['contentStatus', 'content status (cp:contentStatus)'],
  ['language', 'language (dc:language)'],
  ['version', 'version (cp:version)'],
];

/**
 * Post-sanitize package scan: re-open the SAVED bytes —
 * bypassing every in-memory caching layer — and verify the claims the report
 * is about to make. A "Result: CLEAN" verdict over a package that still
 * carries custom properties or an identifier is worse than no sanitizer.
 */
export function verifySanitizedPackage(outBuffer: Buffer): void {
  const unzipped = unzipSync(new Uint8Array(outBuffer));
  const names = Object.keys(unzipped);
  const problems: string[] = [];

  if (names.includes('docProps/custom.xml')) {
    problems.push('docProps/custom.xml (custom document properties) is still in the package');
  }
  if (names.some(n => n.startsWith('customXml/'))) {
    problems.push('customXml/* parts are still in the package');
  }
  if (names.includes('docProps/core.xml')) {
    const core = parseXml(strFromU8(unzipped['docProps/core.xml']));
    for (const [local, label] of VERIFIED_CORE_FIELDS) {
      for (const el of transforms.findDescendantsByLocalName(core.documentElement! as unknown as Element, local)) {
        if ((el.textContent || '').trim()) {
          problems.push(`core property ${label} still contains a value`);
        }
      }
    }
  }
  if (names.includes('word/settings.xml')) {
    // Document variables are invisible metadata (QA ADEU-QA-001): a surviving
    // w:docVar means the strip transform silently failed.
    const settings = parseXml(strFromU8(unzipped['word/settings.xml']));
    if (
      transforms.findDescendantsByLocalName(settings.documentElement! as unknown as Element, 'docVar').length > 0
    ) {
      problems.push('word/settings.xml still contains document variables (w:docVar)');
    }
  }

  if (problems.length > 0) {
    throw new Error(
      'Sanitize integrity check failed — the saved package still contains metadata this run ' +
        'claims to remove:\n  - ' +
        problems.join('\n  - ') +
        '\nRefusing to report a clean document.',
    );
  }
}