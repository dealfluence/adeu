import { describe, it, expect } from 'vitest';
import {
  _replace_smart_quotes,
  _make_fuzzy_regex,
  _find_match_in_text,
  _build_critic_markup,
  apply_edits_to_markdown,
  format_ambiguity_error
} from './markup.js';
import { ModifyText } from './models.js';

describe('Markup Helpers', () => {
  it.each([
    ['"Hello" and \'World\'', '"Hello" and \'World\''],
    ['Smart “quotes” and ‘apostrophes’', '"Hello" and \'World\''.replace('Hello', 'quotes').replace('World', 'apostrophes')], // Workaround for JS literal parsing in table
  ])('replace_smart_quotes(%s)', (input, expected) => {
    // Quick override for the manual table definition above
    if (input.includes('Smart')) expected = 'Smart "quotes" and \'apostrophes\'';
    expect(_replace_smart_quotes(input)).toBe(expected);
  });

  it.each([
    ['hello world', ['hello world', 'hello  world', 'hello   world']],
    ['[___]', ['[___]', '[_____]', '[__________]']],
  ])('make_fuzzy_regex(%s)', (inputStr, matches) => {
    const pattern = new RegExp(_make_fuzzy_regex(inputStr));
    for (const m of matches) {
      expect(m).toMatch(pattern);
    }
  });

  it.each([
    ['The quick brown fox', 'quick', 4, 9],
    ['"Hello" said the fox', '"Hello"', 0, 7],
    ['hello   world', 'hello world', 0, 13],
    ['The quick brown fox', 'elephant', -1, -1],
    ['Some text', '', -1, -1],
  ])('find_match_in_text: %s targets %s', (text, target, expectedStart, expectedEnd) => {
    const [start, end] = _find_match_in_text(text, target);
    expect(start).toBe(expectedStart);
    expect(end).toBe(expectedEnd);
  });
});

describe('build_critic_markup', () => {
  it.each([
    { t: 'old', n: '', expected: '{--old--}' },
    { t: '', n: 'new', expected: '{++new++}' },
    { t: 'old', n: 'new', expected: '{--old--}{++new++}' },
    { t: 'old', n: 'new', c: 'Changed this', expected: '{--old--}{++new++}{>>Changed this<<}' },
    { t: 'old', n: 'new', idx: 3, incIdx: true, expected: '{--old--}{++new++}{>>[Edit:3]<<}' },
    { t: 'old', n: 'new', c: 'Reason', idx: 5, incIdx: true, expected: '{--old--}{++new++}{>>Reason [Edit:5]<<}' },
    { t: 'target', n: 'ignored', highlight: true, expected: '{==target==}' },
    { t: 'target', n: 'ignored', c: 'Note', idx: 2, incIdx: true, highlight: true, expected: '{==target==}{>>Note [Edit:2]<<}' },
    
    // Formatting
    { t: '**Important**', n: '**Critical**', expected: '**{--Important--}{++Critical++}**' },
    { t: '_emphasis_', n: '_strong emphasis_', expected: '_{--emphasis--}{++strong emphasis++}_' },
    { t: '**_nested_**', n: '**_deeply nested_**', expected: '**{--_nested_--}{++_deeply nested_++}**' },
    { t: '**unbalanced', n: '**still unbalanced', expected: '{--**unbalanced--}{++**still unbalanced++}' },
    { t: '__0__', n: '__1__', expected: '{--__0__--}{++__1__++}' },
    
    // Edge Cases
    { t: '', n: '', expected: '' },
    { t: '   ', n: 'text', expected: '{--   --}{++text++}' },
    { t: 'Line1\nLine2', n: 'SingleLine', expected: '{--Line1\nLine2--}{++SingleLine++}' },
    { t: 'C++', n: 'Python', expected: '{--C++--}{++Python++}' },
    { t: 'old', n: 'new', c: '   ', expected: '{--old--}{++new++}{>>   <<}' },
    { t: 'old', n: 'new', c: '', expected: '{--old--}{++new++}' },
    { t: '', n: 'ignored', highlight: true, expected: '' }
  ])('builds correct markup for $t -> $n', ({ t, n, c, idx = 0, incIdx = false, highlight = false, expected }) => {
    const result = _build_critic_markup(t, n, c, idx, incIdx, highlight);
    if (highlight && !t) {
      expect(['', '{====}']).toContain(result);
    } else {
      expect(result).toBe(expected);
    }
  });
});

describe('apply_edits_to_markdown', () => {
  it.each([
    ['Notice of Termination', 'Notice of Termination', 'Notice of Immediate Termination', 'Notice of {++Immediate ++}Termination'],
    ['Hello World', 'Hello World', 'Hello Universe', 'Hello {--World--}{++Universe++}'],
    ['Old Item', 'Old Item', 'New Item', '{--Old--}{++New++} Item'],
    ['Original text', 'none', 'none', 'Original text'],
    ['Remove this word please.', 'this ', '', 'Remove {--this --}word please.'],
    ['', 'x', 'y', ''],
    ['Price is $100.00 (USD).', '$100.00', '$200.00', '{--$100.00--}{++$200.00++}'],
    ['Use {curly} and [square] brackets.', '{curly}', '{braces}', '{--{curly}--}{++{braces}++}']
  ])('basic edge cases: %s', (text, target, newText, expected) => {
    const result = target === 'none' ? apply_edits_to_markdown(text, []) : apply_edits_to_markdown(text, [{ type: 'modify', target_text: target, new_text: newText }]);
    expect(result).toContain(expected);
  });

  it('handles modification with comment', () => {
    const text = 'The quick brown fox.';
    const edits: ModifyText[] = [{ type: 'modify', target_text: 'quick', new_text: 'slow', comment: 'Speed change' }];
    expect(apply_edits_to_markdown(text, edits)).toContain('{--quick--}{++slow++}{>>Speed change<<}');
  });

  it('handles highlight_only mode', () => {
    const text = 'Highlight this section please.';
    const edits: ModifyText[] = [{ type: 'modify', target_text: 'this section', new_text: 'ignored' }];
    const result = apply_edits_to_markdown(text, edits, false, true);
    expect(result).toContain('{==this section==}');
    expect(result).not.toContain('{--');
  });

  it('preserves order of multiple edits', () => {
    const text = 'A B C';
    const edits: ModifyText[] = [
      { type: 'modify', target_text: 'A', new_text: 'X' },
      { type: 'modify', target_text: 'B', new_text: 'Y' },
      { type: 'modify', target_text: 'C', new_text: 'Z' }
    ];
    const result = apply_edits_to_markdown(text, edits, true);
    expect(result).toContain('[Edit:0]');
    expect(result.indexOf('{++X++}')).toBeLessThan(result.indexOf('{++Y++}'));
    expect(result.indexOf('{++Y++}')).toBeLessThan(result.indexOf('{++Z++}'));
  });

  it('skips overlapping edits (first wins)', () => {
    const text = 'The quick brown fox';
    const edits: ModifyText[] = [
      { type: 'modify', target_text: 'quick brown', new_text: 'slow red' },
      { type: 'modify', target_text: 'brown fox', new_text: 'green dog' }
    ];
    const result = apply_edits_to_markdown(text, edits);
    expect(result).toContain('{--quick brown--}{++slow red++}');
    expect(result).not.toContain('green dog');
  });

  it.each([
    ['hello   world', 'hello world', '{--hello   world--}'],
    ['Sign here: [__________]', '[___]', '{--[__________]--}'],
    ['"Hello" said the fox.', '"Hello"', '{--"Hello"--}']
  ])('fuzzy and smart quotes: %s', (text, target, expectedSubstring) => {
    const result = apply_edits_to_markdown(text, [{ type: 'modify', target_text: target, new_text: 'replacement' }]);
    expect(result).toContain(expectedSubstring);
  });

  it.each([
    ['The **quick brown fox** jumped.', 'quick brown fox', 'slow red dog', 'The **{--quick brown fox--}{++slow red dog++}** jumped.'],
    ['This is _emphasized_ text.', 'emphasized', 'highlighted', '_{--emphasized--}{++highlighted++}_'],
    ['Variable __init__ is special.', '__init__', '__setup__', '__{--init--}{++setup++}__'],
  ])('formatting noise and preservation: %s', (text, target, newText, expectedSubstring) => {
    const result = apply_edits_to_markdown(text, [{ type: 'modify', target_text: target, new_text: newText }]);
    expect(result).toContain(expectedSubstring);
  });
});

describe('format_ambiguity_error (Turn Loop Trap mitigation)', () => {
  // Mirror the DPA scenario: the same placeholder appears in multiple clauses.
  const haystack =
    'PROVIDER: [official company name] shall process the data. ' +
    'PROVIDER: [official company name] is the data processor.';
  const target = 'PROVIDER: [official company name]';
  const positions: [number, number][] = [
    [0, target.length],
    [58, 58 + target.length],
  ];

  it('names BOTH match_mode escape hatches so the agent can re-call instead of looping', () => {
    const msg = format_ambiguity_error(1, target, haystack, positions);

    expect(msg).toContain('Edit 1 Failed: Ambiguous match');
    expect(msg).toContain('appears 2 times');

    // The actionable remediation that breaks the loop.
    expect(msg).toContain('"match_mode": "all"');
    expect(msg).toContain('"match_mode": "first"');
    expect(msg).toContain('ALL 2 occurrences');
    expect(msg).toContain('FIRST occurrence');

    // The original "add more context" guidance is preserved as a third option.
    expect(msg).toContain('Provide more surrounding context');
  });

  it('still throws for fewer than two matches', () => {
    expect(() => format_ambiguity_error(1, target, haystack, [[0, 5]])).toThrow(
      /requires at least 2 matches/,
    );
  });
});