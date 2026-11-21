import { div } from "gpui-kit";
import { h_flex, v_flex, TextView } from "gpui-base";
import { theme } from "herdr";
import { PullRequestClient } from "./pr-client.js";
import { reviewLabel } from "./pr-model.js";

const http = url => /^https:\/\//i.test(url || "");
const secondary = text => div().text_size(12).text_color(theme().muted).child(text);
const tone = kind => kind === "failed" ? theme().ansi1 : kind === "passed" ? theme().ansi2 : theme().muted;

export class PullRequests {
  constructor(execute, where) { this.execute = execute; this.reset(where); }

  reset(where) {
    this.client = new PullRequestClient(this.execute, where);
    this.token = this.previous = null;
    this.loading = false;
  }

  cancel() { this.token = null; this.loading = false; }

  refresh(host, cx, quiet = false) {
    if (quiet && this.loading) return;
    const token = this.token = {}, client = this.client;
    this.loading = true;
    cx.notify();
    cx.spawn(async cx => {
      try {
        const result = await client.current(this.previous, !quiet);
        if (this.token !== token || host.source !== "pr") return;
        host.applyCurrentPullRequest(result, cx, () => this.token === token);
      } catch (error) {
        if (this.token === token) {
          const p = error.problem;
          host.error = p ? [p.title, p.detail, p.command, p.raw].filter(Boolean).join(" · ") : error.message;
        }
      } finally {
        if (this.token === token) this.loading = false;
        cx.notify();
      }
    });
  }

  description(host, pr) {
    const t = theme();
    return v_flex().id(`pr-description-${pr.url}`).flex_1().min_w(0).min_h(0).overflow_y_scrollbar().px(14).py(12).gap(14)
      .child(div().text_size(13).font_semibold().child(pr.title))
      .child(secondary(`${pr.author?.login || "ghost"} · ${pr.headRefName} → ${pr.baseRefName} · ${pr.state?.toLowerCase() || "open"}`))
      .child(div().text_size(12).text_color(tone(pr.checks?.kind)).child(`${reviewLabel(pr)} · ${pr.checks?.label || "No checks"}`))
      .when(pr.mergeable === "CONFLICTING", el => el.child(div().text_size(12).text_color(t.ansi1).child("This pull request has merge conflicts.")))
      .child(TextView.markdown(`pr-body-${pr.number}`, pr.body || "No description provided.").selectable(true).w_full().text_size(12).text_color(t.foreground)
        .on_link_click((url, cx) => { if (http(url)) cx.open_url(url); }))
      .when(pr.bodyTruncated, el => el.child(secondary("Description shortened. Open GitHub for the full text.")))
      .when(Boolean(pr.checksList?.length), el => el.child(v_flex().gap(6)
        .child(div().text_size(12).font_semibold().child("Checks"))
        .children(pr.checksList.slice(0, 80).map((check, i) => h_flex().gap(8).items_center()
          .child(div().flex_1().min_w(0).text_size(12).child(`${check.name} · ${check.state.toLowerCase()}`))
          .when(http(check.url), el => el.child(host.button(`pr-check-${i}`, "Open", cx => cx.open_url(check.url), { small: true })))))
        .when(pr.checksList.length > 80, el => el.child(secondary("First 80 checks shown. Open GitHub for all checks.")))));
  }

}
