declare namespace App {
  interface Locals {
    /**
     * Demo holder mode only (src/lib/demoHolder.ts): the signed-in holder, or null when signed out.
     * Undefined outside the local live demo.
     */
    demoHolder?: string | null;
  }
}
