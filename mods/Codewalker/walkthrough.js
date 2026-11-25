import { div, svg } from "gpui-kit";
import { h_flex, v_flex, v_resizable, resizable_panel, Button, TextView } from "gpui-base";
import { theme, settings } from "herdr";
import { excerptRows, viewLabel, stepActivity, sortWalkthroughs } from "./walkthrough-model.js";
import { splitPath, timestamp, colorRows } from "./model.js";

// The walkthrough pane reuses Review's parts: its buttons, diff rows, message
// views, composer and Send. The host owns all state and every write.
const stop = (_event, cx) => cx.stop_propagation();
const ICON = Object.fromEntries(["book-open", "chevron-left", "chevron-right", "message", "reply", "trash", "check"].map(name => [name, `mods/Codewalker/icons/${name}.svg`]));
const http = url => /^https?:\/\//i.test(url || "");

function markdown(id, text, size, color) {
  return TextView.markdown(id, text).selectable(true).w_full().text_size(size).text_color(color)
    .on_link_click((url, cx) => { if (http(url)) cx.open_url(url); });
}

// Review's list of this checkout and branch's walkthroughs.
export function walkthroughList(host) {
  const t = theme(), list = sortWalkthroughs(host.walkthroughs);
  const helper = host.client.helperPath(host.context());
  return v_flex().flex_1().min_h(0).min_w(0)
    .child(h_flex().h(40).flex_none().px(14).gap(8).items_center().border_b(1).border_color(t.border)
      .child(svg(ICON["book-open"]).size(13).flex_none().text_color(t.muted))
      .child(div().font_semibold().text_size(12).child("Walkthroughs"))
      .child(div().text_size(11).text_color(t.dim).child(String(list.length))))
    .child(v_flex().id("review-walkthroughs").flex_1().min_h(0).overflow_y_scrollbar().px(6).py(6).gap(1)
      .children(list.map(walkthrough => {
        const archived = walkthrough.status === "archived";
        const meta = [`${walkthrough.stepCount} ${walkthrough.stepCount === 1 ? "step" : "steps"}`, `${walkthrough.replies} ${walkthrough.replies === 1 ? "reply" : "replies"}`,
          walkthrough.author?.name, timestamp(walkthrough.updatedAt)].filter(Boolean).join(" · ");
        return Button.new(`walkthrough-${walkthrough.id}`).w_full().flex_none().px(8).py(8).rounded(6).cursor_pointer()
          .text_color(archived ? t.muted : t.foreground).hover(s => s.bg(t.hover).text_color(t.foreground))
          .accessibility_label(`Open walkthrough ${walkthrough.title}`).on_mouse_down("left", stop)
          .on_click((_event, cx) => host.openWalkthrough(walkthrough.id, cx))
          .child(v_flex().w_full().min_w(0).gap(3)
            .child(host.wrapHeader([
              div().min_w(0).truncate().font_semibold().text_size(12).line_height("18px").child(walkthrough.title),
              ...(walkthrough.awaitingReply ? [host.tag("Awaiting reply", t.working)] : []),
              ...(archived ? [host.tag("Archived", t.muted, t.raised)] : []),
            ], []))
            .child(div().min_w(0).truncate().text_size(11).line_height("16px").text_color(t.dim).child(meta)));
      }))
      .when(!list.length, el => el.child(host.emptyState("No walkthroughs",
        `Ask an agent for one. It learns how with: python3 ${helper ?? "<helper>"} skill --topic walkthrough`))));
}

// Title, summary, actions and the numbered steps.
function outline(host, walkthrough, at, wide, cx) {
  const t = theme(), archived = walkthrough.status === "archived", confirming = host.walkthroughConfirm === walkthrough.id;
  const stale = walkthrough.steps.filter(step => step.validity === "stale").length;
  const busy = host.saving || host.leaving || Boolean(host.postAttempt);
  const steps = walkthrough.steps.map((step, index) => {
    const thread = host.stepThread(step), activity = stepActivity(thread), on = index === at, label = viewLabel(step.view);
    const tone = activity.awaiting ? t.working : activity.resolved ? t.dim : t.accent;
    return Button.new(`walkthrough-step-${index}`).w_full().flex_none().px(8).py(7).rounded(6).cursor_pointer()
      .bg(on ? t.selected : "#00000000").text_color(on ? t.foreground : t.muted)
      .hover(s => s.bg(on ? t.selected : t.hover).text_color(t.foreground))
      .accessibility_label(`Step ${index + 1}: ${step.title}`).on_mouse_down("left", stop).on_click((_event, cx) => host.goStep(index, cx))
      .child(h_flex().w_full().gap(8).items_center()
        .child(div().w(18).flex_none().text_size(11).font_family(settings().font_family).text_color(on ? t.accent : t.dim).child(String(index + 1)))
        .child(v_flex().flex_1().min_w(0).gap(2)
          .child(div().min_w(0).truncate().text_size(12).line_height("18px").child(step.title))
          .when(Boolean(label), el => el.child(div().min_w(0).truncate().text_size(11).line_height("16px").text_color(t.dim).child(`${splitPath(label.path).name} · ${label.detail}`))))
        .when(step.validity === "stale", el => el.child(div().id(`walkthrough-stale-${index}`).size(6).flex_none().rounded_full().bg(t.ansi1).tooltip("The code changed since this step was written")))
        .when(activity.replies > 0 || activity.awaiting, el => el.child(h_flex().flex_none().gap(3).items_center().text_size(11).text_color(tone)
          .child(svg(ICON.message).size(11).text_color(tone)).child(String(activity.replies)))));
  });
  return v_flex().flex_none().min_h(0).border_color(t.border)
    .when(wide, el => el.w(host.width >= 1000 ? 300 : 260).border_r(1))
    .when(!wide, el => el.h_full().w_full())
    .child(v_flex().id("walkthrough-outline").flex_1().min_h(0).overflow_y_scrollbar()
      .child(v_flex().px(14).pt(12).pb(10).gap(6).border_b(1).border_color(t.border)
        .child(div().font_semibold().text_size(13).child(walkthrough.title))
        .child(host.wrapHeader([
          div().min_w(0).truncate().text_size(11).text_color(t.dim).child([walkthrough.author?.name, timestamp(walkthrough.createdAt)].filter(Boolean).join(" · ")),
          ...(archived ? [host.tag("Archived", t.muted, t.raised)] : []),
          ...(stale ? [host.tag(`${stale} changed since`, t.ansi1)] : []),
        ], []))
        .when(Boolean(walkthrough.summary?.trim()), el => el.child(markdown(`walkthrough-summary-${walkthrough.id}`, walkthrough.summary, 12, t.muted)))
        .child(confirming
          ? v_flex().gap(6).pt(2)
            .child(div().text_size(11).text_color(t.ansi1).child("Delete this walkthrough, its steps and every reply? This cannot be undone."))
            .child(h_flex().gap(6)
              .child(host.button("walkthrough-keep", "Keep", cx => { host.walkthroughConfirm = null; cx.notify(); }, { small: true, ghost: true, enabled: !host.saving }))
              .child(host.button("walkthrough-confirm-delete", host.saving ? "Deleting…" : "Delete walkthrough", cx => host.deleteWalkthrough(cx), { small: true, danger: true, icon: ICON.trash, enabled: !host.saving })))
          : h_flex().gap(6).pt(2)
            .child(host.button("walkthrough-archive", archived ? "Reopen" : "Archive", cx => host.setWalkthroughStatus(archived ? "open" : "archived", cx),
              { small: true, icon: archived ? null : ICON.check, enabled: !busy, tooltip: archived ? "Move back to open walkthroughs" : "Move to the archived walkthroughs; nothing is deleted" }))
            .child(host.button("walkthrough-delete", "Delete", cx => host.deleteWalkthrough(cx), { small: true, ghost: true, icon: ICON.trash, enabled: !busy, label: "Delete this walkthrough" }))))
      .child(v_flex().px(6).py(6).gap(1).children(steps)));
}

function excerpt(host, step) {
  const t = theme(), label = viewLabel(step.view), rows = colorRows(excerptRows(step.view), step.view?.path);
  if (!label || !rows.length) return null;
  const { name, dir } = splitPath(label.path), stale = step.validity === "stale";
  return v_flex().id(`walkthrough-excerpt-${step.id}`).mx(16).my(8).rounded(8).border_1().border_color(t.border).overflow_hidden()
    .child(h_flex().min_h(32).px(12).py(5).gap(8).flex_wrap().items_center().rounded_t(7).bg(t.raised).border_b(1).border_color(t.border)
      .child(div().flex_none().font_semibold().text_size(12).child(name))
      .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(t.dim).child(dir))
      .child(div().flex_none().text_size(11).text_color(t.muted).child(label.detail))
      .when(stale, el => el.child(host.tag("Code changed since", t.ansi1))))
    .when(stale, el => el.child(div().px(12).py(6).text_size(11).text_color(t.muted).border_b(1).border_color(t.border)
      .child("These are the lines as they were when the step was written.")))
    .children(rows.map((row, index) => host.diffRow(row, index, false, false, index === rows.length - 1 ? 7 : 0)));
}

function discussion(host, step, thread, cx) {
  const t = theme(), activity = stepActivity(thread), replies = thread.messages.slice(1);
  const composing = host.intent?.threadId === thread.id, draft = (host.drafts ?? []).some(item => item.intent?.threadId === thread.id);
  const enabled = !host.saving && !host.leaving;
  return v_flex().id(`walkthrough-discussion-${thread.id}`).mx(16).mt(14).gap(10)
    .child(host.wrapHeader([
      div().flex_none().font_semibold().text_size(12).child("Discussion"),
      div().flex_none().text_size(11).text_color(t.dim).child(String(replies.length)),
      ...(activity.resolved ? [host.tag("Resolved", t.muted, t.raised)] : []),
      ...(activity.awaiting ? [host.tag("Awaiting reply", t.working)] : []),
    ], [
      host.button(`step-status-${thread.id}`, activity.resolved ? "Reopen" : "Resolve", cx => host.setStatus(thread, cx), { small: true, icon: activity.resolved ? null : ICON.check, enabled }),
    ]))
    .when(replies.length > 0, el => el.child(v_flex().pl(12).gap(12).border_l(2).border_color(t.border)
      .children(replies.map(message => host.messageView(message, "step")))))
    .when(!replies.length && !composing, el => el.child(div().text_size(12).text_color(t.muted).child("No replies yet. Ask the agent about this step; Send delivers your replies.")))
    .when(composing, el => el.child(host.composerView(false)))
    .when(!composing && !activity.resolved, el => el.child(h_flex().gap(6)
      .child(host.button(`step-reply-${thread.id}`, draft ? "Continue draft" : "Reply", cx => host.beginStepReply(thread, cx), { small: true, ghost: true, icon: ICON.reply, enabled }))));
}

function stepView(host, walkthrough, at, compact, cx) {
  const t = theme(), steps = walkthrough.steps, step = steps[at], thread = host.stepThread(step), body = thread.messages[0];
  const previous = steps[at - 1], next = steps[at + 1];
  return v_flex().flex_1().min_h(0).min_w(0)
    .child(v_flex().id(`walkthrough-content-${step.id}`).flex_1().min_h(0).overflow_y_scrollbar().pb(20)
      .child(v_flex().px(16).pt(14).pb(6).gap(8)
        .child(div().text_size(11).text_color(t.dim).child(`Step ${at + 1} of ${steps.length}`))
        .child(div().text_size(15).font_semibold().child(step.title))
        .when(Boolean(body), el => el.child(markdown(`walkthrough-body-${body.id}`, body.body, 13, t.foreground)))
        .when(Boolean(body?.editedAt), el => el.child(div().text_size(10).text_color(t.dim).child(`Edited ${timestamp(body.editedAt)}`))))
      .child(excerpt(host, step) ?? div())
      .child(discussion(host, step, thread, cx)))
    .child(h_flex().h(40).flex_none().px(12).gap(8).items_center().border_t(1).border_color(t.border)
      .child(host.button("walkthrough-previous", compact ? "" : "Previous", cx => host.goStep(at - 1, cx),
        { small: true, icon: ICON["chevron-left"], enabled: Boolean(previous), label: "Previous step", tooltip: previous ? `${at}. ${previous.title}` : "This is the first step" }))
      .child(div().flex_1().min_w(0).truncate().text_size(11).text_color(t.muted).text_center().child(next ? `Next: ${next.title}` : "Last step"))
      .child(host.button("walkthrough-next", compact ? "" : "Next", cx => host.goStep(at + 1, cx),
        { small: true, icon: ICON["chevron-right"], enabled: Boolean(next), label: "Next step", tooltip: next ? `${at + 2}. ${next.title}` : "This is the last step" })));
}

// Outline beside the step when wide; stacked with a native divider when narrow.
export function walkthroughBody(host, { wide, compact }, cx) {
  const walkthrough = host.walkthrough;
  if (!walkthrough) return v_flex().id("walkthrough-empty").flex_1().min_h(0).child(host.error ? div()
    : host.emptyState("Loading walkthrough…", "", true));
  if (!walkthrough.steps.length) return v_flex().id("walkthrough-empty").flex_1().min_h(0).child(host.emptyState("This walkthrough has no steps", ""));
  const at = Math.max(0, Math.min(host.step, walkthrough.steps.length - 1));
  const side = outline(host, walkthrough, at, wide, cx), content = stepView(host, walkthrough, at, compact, cx);
  if (wide) return h_flex().items_stretch().flex_1().min_h(0).min_w(0).child(side).child(content);
  // The title block takes about 150px; then one 40px row per step, up to a bit over a third of the height.
  const outlineH = Math.round(Math.min(150 + walkthrough.steps.length * 40, Math.max(160, host.height * 0.38)));
  return v_resizable("walkthrough-stacked").flex_1().min_h(0).min_w(0).w_full()
    .child(resizable_panel().size(outlineH).size_range(96).child(side))
    .child(resizable_panel().size_range(160).child(content));
}
