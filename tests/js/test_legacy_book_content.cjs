const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  children = [];
  dataset = {};
  listeners = new Map();

  constructor(tagName) {
    this.tagName = tagName;
  }

  set innerHTML(value) {
    assert.fail(`Stored content was sent to an HTML parser: ${value}`);
  }

  set onclick(handler) {
    assert.fail(`An inline event handler was assigned: ${handler}`);
  }

  set textContent(value) {
    this.children = value ? [String(value)] : [];
  }

  get textContent() {
    return this.children.map(child => typeof child === 'string' ? child : child.textContent).join('');
  }

  append(...children) {
    this.children.push(...children);
  }

  appendChild(child) {
    this.append(child);
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }
}

const html = fs.readFileSync(path.join(__dirname, '../../app/tts_v2.html'), 'utf8');
const title = '<script>example</script><br>question';
const option = `A'"><img src="x" onerror="example">`;
const answer = '<a href="javascript:example">answer</a>';
const elements = {
  examQuestions: new Element('div'),
  examSubmitBtn: new Element('button'),
  assistantMessages: new Element('div'),
};
const selected = [];
const spoken = [];
const context = vm.createContext({
  assistantOpen: true,
  examData: [{ 问题: title, 选项: { [option]: answer } }],
  userAnswers: {},
  document: {
    getElementById: id => elements[id],
    createElement: tag => new Element(tag),
  },
  selectOption: (...args) => selected.push(args),
  scrollAssistantToBottom() {},
  playBaikeQA(question, response, onQuestionEnd) {
    spoken.push(question, response);
    onQuestionEnd();
  },
});

for (const [functionName, next] of [
  ['function renderExam(', '// 选择选项'],
  ['function handleBaikeDanmuClick(', '// 播放百科问答TTS'],
]) {
  const start = html.indexOf(functionName);
  const end = html.indexOf(next, start);
  assert(start >= 0 && end > start);
  vm.runInContext(html.slice(start, end), context);
}

context.renderExam();
const [, question, options] = elements.examQuestions.children[0].children;
assert.deepEqual(question.children, [title]);
const [button] = options.children;
assert.equal(button.dataset.answer, option);
assert.deepEqual(button.children[0].children, [`${option}.`]);
assert.equal(button.children[1], ` ${answer}`);
button.listeners.get('click')();
assert.deepEqual(selected, [[0, option]]);

context.handleBaikeDanmuClick({ question: title, answer });
const [questionMsg, answerMsg] = elements.assistantMessages.children;
assert.equal(questionMsg.children[1].tagName, 'br');
assert.equal(questionMsg.children[2], title);
assert.equal(answerMsg.children[2], answer);
assert.deepEqual(spoken, [`对于问题，${title}`, `答案是，${answer}`]);
console.log('Legacy exam and encyclopedia render stored content as text and preserve their actions.');
