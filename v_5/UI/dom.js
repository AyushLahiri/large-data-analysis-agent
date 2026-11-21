/**
 * Create an element with optional plain text and CSS classes.
 *
 * Inputs: tag: element name; text: optional text; className: optional classes.
 * Returns: Unattached DOM element.
 * Details: Shared rendering helper. textContent treats supplied text as text, not HTML.
 */
function el(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
