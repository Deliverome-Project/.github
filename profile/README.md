<p align="center">
  <img src="./deliverome_sticker_horizontal_caps.svg" alt="Deliverome" width="420">
</p>

**Deliverome** is a nonprofit startup building an open atlas of the human surfaceome — abundance, specificity, internalization, and routing — to scale targeted delivery beyond the liver.

### Start here

- 🌐 [deliverome.org](https://deliverome.org): who we are and what we're building
- 🔬 [Surfaceome viewer](https://surfaceome.deliverome.org): browse the atlas protein by protein
- 📄 [The accessible human surfaceome](https://deliverome.org/paper/accessible-surfaceome): the paper behind the atlas
- ✍️ [Substack](https://deliverome.substack.com): posts on our data and methods

### Repositories

**Data and atlases**
- [`accessible-surfaceome`](https://github.com/Deliverome-Project/accessible-surfaceome): an open atlas of which human proteins reach the cell surface, and in what context, with a browsable viewer, a JSON API, and a literature citation behind every call
- [`deliverome-surfaceome-library-v1`](https://github.com/Deliverome-Project/deliverome-surfaceome-library-v1): a barcoded, epitope-tagged ORF library of human cell-surface proteins, with its design brief and an offline reproduction of every design decision

**Analysis tools**
- [`agentflow`](https://github.com/Deliverome-Project/agentflow): reproducible flow cytometry analysis with FlowKit, a CLI, and optional Matplotlib gate editing
- [`limpa-rs`](https://github.com/Deliverome-Project/limpa-rs): Rust-accelerated LIMPA protein quantification with a pinned R reference and Python interface (release candidate)

**Writing**
- [`deliverome-blog`](https://github.com/Deliverome-Project/deliverome-blog): code and data behind our Substack posts

### How we work

We publish our data and code openly. Each repository's `LICENSE` and `CITATION.cff` give the exact terms and how to cite it:

| Repository | Licence | Cite |
|---|---|---|
| `accessible-surfaceome` | Code MIT; derived data CC0 1.0 | Software [10.5281/zenodo.22116981](https://doi.org/10.5281/zenodo.22116981); data [10.5281/zenodo.20805383](https://doi.org/10.5281/zenodo.20805383) |
| `deliverome-surfaceome-library-v1` | Code MIT; library designs CC0 1.0 | `CITATION.cff` |
| `agentflow` | MIT | [10.5281/zenodo.23718421](https://doi.org/10.5281/zenodo.23718421), plus the FlowKit paper |
| `limpa-rs` | GPL-3.0-or-later | The original LIMPA papers, listed in `CITATION.cff` |
| `deliverome-blog` | Code MIT; data CC0 1.0; text and figures CC BY 4.0 | Each post's own Zenodo DOI, in the repo README |

Third-party data keeps its original licence, recorded in each repository's licensing notes.

📬 Questions, collaborations, or data requests: [contact@deliverome.org](mailto:contact@deliverome.org)
