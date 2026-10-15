/**
 * 「整理断行」算法的单测。
 *
 * 算法本身住在 static/index.html 里（这东西要的就是单文件，不拆），
 * 这里把 TIDY-START ~ TIDY-END 之间那段源码抠出来跑 —— 它是纯函数，
 * 不碰 DOM，所以能直接在 node 里测。
 *
 * 跑：node test_tidy.mjs
 */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, 'static', 'index.html'), 'utf8');

const block = html.match(/\/\* TIDY-START \*\/([\s\S]*?)\/\* TIDY-END \*\//);
if (!block) throw new Error('index.html 里找不到 TIDY-START/TIDY-END 标记 —— 算法块被挪走或标记被删了');

const { tidyText } = new Function(`${block[1]}; return { tidyText };`)();

const tidy = (s) => tidyText(s).text;
const merges = (s) => tidyText(s).merges;

// ─── 核心：被硬换行切断的句子要接回去 ──────────────────────

test('中文句子被切成两行，接回去且不插空格', () => {
  assert.equal(tidy('这是一句被切断的\n很长的话'), '这是一句被切断的很长的话');
});

test('逗号、顿号结尾正是断句特征，要接', () => {
  assert.equal(tidy('前面说了一半，\n后面接着说'), '前面说了一半，后面接着说');
  assert.equal(tidy('苹果、\n香蕉'), '苹果、香蕉');
});

test('句号结尾是真的说完了，不接', () => {
  assert.equal(tidy('第一句话。\n第二句话。'), '第一句话。\n第二句话。');
  assert.equal(tidy('问完了吗？\n问完了'), '问完了吗？\n问完了');
  assert.equal(tidy('Done.\nNext line'), 'Done.\nNext line');
});

test('冒号、分号后面通常是列表或下一项，不接', () => {
  assert.equal(tidy('注意：\n这是正文'), '注意：\n这是正文');
  assert.equal(tidy('第一项；\n第二项'), '第一项；\n第二项');
});

test('句末标点后带收尾引号、括号，照样算说完了', () => {
  assert.equal(tidy('他说「好的。」\n然后走了'), '他说「好的。」\n然后走了');
});

// ─── 空行、段落 ───────────────────────────────────────────

test('空行是真的分段，两边不许接到一起', () => {
  assert.equal(tidy('第一段没写完\n\n第二段'), '第一段没写完\n\n第二段');
});

test('连着几个空行压成一个，首尾空行扔掉', () => {
  assert.equal(tidy('\n\n开头\n\n\n\n中间\n\n'), '开头\n\n中间');
});

// ─── 不该碰的东西 ─────────────────────────────────────────

test('列表项自成一行，不会被上一行吞掉', () => {
  assert.equal(tidy('要买的东西\n- 苹果\n- 香蕉'), '要买的东西\n- 苹果\n- 香蕉');
  assert.equal(tidy('步骤如下\n1. 打开\n2. 关上'), '步骤如下\n1. 打开\n2. 关上');
  assert.equal(tidy('清单\n• 甲\n• 乙'), '清单\n• 甲\n• 乙');
  assert.equal(tidy('清单\n① 甲\n② 乙'), '清单\n① 甲\n② 乙');
});

test('标题既不被接走，也不接走下一行', () => {
  assert.equal(tidy('# 标题\n正文第一行'), '# 标题\n正文第一行');
  assert.equal(tidy('前一行没写完\n## 二级标题'), '前一行没写完\n## 二级标题');
});

test('引用和表格行保持原样', () => {
  assert.equal(tidy('他说\n> 引用的话'), '他说\n> 引用的话');
  assert.equal(tidy('| 甲 | 乙 |\n| 丙 | 丁 |'), '| 甲 | 乙 |\n| 丙 | 丁 |');
});

test('代码围栏里一行都不许动', () => {
  const src = '说明文字\n```js\nconst a = 1\nconst b = 2\n```\n后面的话';
  assert.equal(tidy(src), src);
});

test('代码块结束后，围栏行不会把下一行接上来', () => {
  const src = '```\ncode\n```\n正文';
  assert.equal(tidy(src), src);
});

// ─── 西文的特殊照顾 ───────────────────────────────────────

test('英文之间补一个空格，不然两个词会黏成一个', () => {
  assert.equal(tidy('this is a broken\nsentence'), 'this is a broken sentence');
});

test('PDF 里被连字符拆开的单词要拼回去', () => {
  assert.equal(tidy('inter-\nnational'), 'international');
});

test('网址被切断，中间绝不能插空格', () => {
  assert.equal(tidy('见 https://example.com/very\nlong/path'), '见 https://example.com/verylong/path');
});

test('中文和英文相邻时直接接，不自作主张加空格', () => {
  assert.equal(tidy('这是中文\nEnglish'), '这是中文English');
});

test('下一行以小写字母开头，就是被切断的后半截，句号也拦不住', () => {
  assert.equal(tidy('The meeting ended.\nand we went home'), 'The meeting ended. and we went home');
  assert.equal(tidy('something broken\nacross two lines'), 'something broken across two lines');
});

test('缩写词后面那个点不是句号', () => {
  assert.equal(tidy('I spoke with Dr.\nSmith yesterday'), 'I spoke with Dr. Smith yesterday');
  assert.equal(tidy('Apple Inc.\nreported earnings'), 'Apple Inc. reported earnings');
  assert.equal(tidy('fruits, e.g.\nApples and pears'), 'fruits, e.g. Apples and pears');
  assert.equal(tidy('based in the U.S.\nMarket share grew'), 'based in the U.S. Market share grew');
});

test('定宽排版（PDF / 邮件）里，满行的句号只是排到了行末，要接回去', () => {
  const pdf = [
    'The quarterly report shows that revenue increased by',
    'twelve percent compared to the same period last year.',
    'Management attributes this growth to the expansion of',
    'the retail network across three additional provinces.',
    'The board has approved a further investment plan that',
    'will be executed over the coming eighteen months.',
  ].join('\n');
  const got = tidy(pdf);
  assert.equal(got.split('\n').length, 1, '定宽段落应该接成一整段：\n' + got);
  assert.ok(got.includes('last year. Management'), '跨句的行也要接上：' + got);
});

test('一行一句的短文本不会被当成定宽排版乱接', () => {
  const src = 'First.\nSecond.\nThird.\nFourth.\nFifth.\nSixth.';
  assert.equal(tidy(src), src);
});

// ─── 清理类 ───────────────────────────────────────────────

test('行尾空白和零宽字符清掉', () => {
  assert.equal(tidy('一行   \n接上'), '一行接上');
  assert.equal(tidy('带零宽​字符。'), '带零宽字符。');
});

test('\\r\\n 换行照样认', () => {
  assert.equal(tidy('被切断的\r\n一句话'), '被切断的一句话');
});

// ─── 计数与边界 ───────────────────────────────────────────

test('merges 如实反映接了几处', () => {
  assert.equal(merges('甲\n乙\n丙'), 2);
  assert.equal(merges('甲。\n乙。'), 0);
});

test('空输入不炸', () => {
  assert.equal(tidy(''), '');
  assert.equal(tidy('   '), '');
  assert.equal(merges(''), 0);
});

test('整理两次和整理一次结果一样（幂等）', () => {
  const src = '从 PDF 粘来的\n一段话被切成\n好几行。\n\n- 列表\n- 还是列表\n\n# 标题\n正文继续';
  const once = tidy(src);
  assert.equal(tidy(once), once);
});

test('除了空白，一个字都不能丢', () => {
  const src = '第一句被切断\n接上了，然后\n- 列表项\n\n# 标题\n正文。';
  const strip = (s) => s.replace(/\s/g, '');
  assert.equal(strip(tidy(src)), strip(src));
});

test('已经排好的文案，整理后原样不动', () => {
  const src = '这是完整的第一段。\n\n这是完整的第二段。';
  assert.equal(tidy(src), src);
});
