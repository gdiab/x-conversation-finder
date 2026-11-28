import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

afterEach(() => {
  cleanup();
});

// Any test that forgets to mock the API client fails loudly instead of
// reaching the network.
vi.stubGlobal(
  'fetch',
  vi.fn(() => {
    throw new Error('Unexpected fetch in a frontend test: mock ../api/client instead.');
  }),
);

// jsdom has no showModal/close on <dialog> (Outpost's Dialog, used by the
// review detail view, issue #50). A minimal stand-in, as in Outpost's own
// test setup: open state, the close event, and Escape firing `cancel` on the
// topmost open dialog the way a browser does.
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  const modalStack: HTMLDialogElement[] = [];

  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
    modalStack.push(this);
  };

  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement, returnValue?: string) {
    this.open = false;
    if (returnValue !== undefined) this.returnValue = returnValue;
    const idx = modalStack.indexOf(this);
    if (idx !== -1) modalStack.splice(idx, 1);
    this.dispatchEvent(new Event('close'));
  };

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    while (modalStack.length > 0 && !modalStack[modalStack.length - 1]!.isConnected) {
      modalStack.pop();
    }
    const top = modalStack[modalStack.length - 1];
    if (top == null) return;
    const cancelEvent = new Event('cancel', { cancelable: true });
    if (top.dispatchEvent(cancelEvent)) top.close();
  });
}
