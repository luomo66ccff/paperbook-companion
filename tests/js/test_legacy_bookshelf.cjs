const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  children = [];
  listeners = new Map();

  set innerHTML(value) {
    assert.fail(`The renderer passed content to innerHTML: ${value}`);
  }

  set onclick(handler) {
    assert.fail(`The renderer assigned an onclick handler: ${handler}`);
  }

  append(...children) {
    for (const child of children) {
      child.parent = this;
      this.children.push(child);
    }
  }

  appendChild(child) {
    this.append(child);
  }

  querySelectorAll(selector) {
    return this.children.filter(child => selector === `.${child.className}`);
  }

  remove() {
    this.parent.children = this.parent.children.filter(child => child !== this);
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  click() {
    let stopped = false;
    this.listeners.get('click')?.({ stopPropagation: () => { stopped = true; } });
    if (!stopped) this.parent?.click();
  }
}

const html = fs.readFileSync(path.join(__dirname, '../../app/tts_v2.html'), 'utf8');
const start = html.indexOf('function renderBookshelfTags()');
const end = html.indexOf('async function removeFromBookshelf(', start);
assert(start >= 0 && end > start);

const container = new Element();
const example = new Element();
const staleTag = new Element();
staleTag.className = 'bookshelf-tag';
container.append(example, staleTag);

const book = {
  name: "O'Reilly <img src=x> & friends",
  session_dir: "bookshelf\\O'Reilly <chapter> & friends",
};
const selected = [];
const removed = [];
const context = vm.createContext({
  bookshelfData: [book],
  document: {
    querySelector: selector => {
      assert.equal(selector, '.examples');
      return container;
    },
    createElement: () => new Element(),
  },
  fillFromBookshelf: value => selected.push(value),
  removeFromBookshelf: value => removed.push(value),
});
new vm.Script(html.slice(start, end)).runInContext(context);
context.renderBookshelfTags();

const [remainingExample, tag] = container.children;
assert.equal(container.children.length, 2);
assert.equal(remainingExample, example);
assert.notEqual(tag, staleTag);
const [, name, remove] = tag.children;
assert.equal(name.className, 'tag-name');
assert.equal(name.textContent, book.name);
assert.equal(name.children.length, 0);
assert.equal(remove.className, 'remove-btn');

remove.click();
assert.deepEqual(removed, [book.session_dir]);
assert.deepEqual(selected, []);
tag.click();
assert.deepEqual(selected, [book]);
console.log('Legacy bookshelf renders literal text and preserves both click actions.');
