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
