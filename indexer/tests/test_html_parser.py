from indexer.parse.html_parser import parse_html


def test_parser_removes_noise_and_preserves_structured_content() -> None:
    parsed = parse_html(
        """
        <article class="ltx_document"><h1 class="ltx_title_document">Paper</h1>
          <div class="ltx_abstract"><div class="ltx_para">Abstract <math alttext="x^2"/></div></div>
          <section class="ltx_section">
            <h2 class="ltx_title"><span class="ltx_tag">3</span> Method</h2>
            <div class="ltx_para">Method text.</div>
            <section class="ltx_subsection">
              <h3 class="ltx_title">
                <span class="ltx_tag">3.2</span> Index construction
              </h3>
              <div class="ltx_para">Build an index.</div>
              <table class="ltx_tabular"><tr><td>a</td><td>b</td></tr></table>
              <figcaption>A caption.</figcaption>
            </section>
          </section>
          <section class="ltx_bibliography">
            <div class="ltx_para">Never include reference.</div>
          </section>
          <section class="ltx_section">
            <h2 class="ltx_title">Acknowledgements</h2>
            <div class="ltx_para">Thanks.</div>
          </section>
          <aside class="ltx_note">Footnote text.</aside>
        </article>
        """
    )

    assert parsed is not None
    assert parsed.sections[0].path == "Abstract"
    assert "$x^2$" in parsed.sections[0].text
    subsection = next(
        section for section in parsed.sections if "Index construction" in section.path
    )
    assert subsection.path == "3 Method > 3.2 Index construction"
    assert "a | b" in subsection.text
    assert "Figure: A caption." in subsection.text
    assert "Never include" not in parsed.raw_text
    assert "Footnote" not in parsed.raw_text
    assert "Thanks" not in parsed.raw_text
