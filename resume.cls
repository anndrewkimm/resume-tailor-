%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% Resume Class
% Reconstructed for resume-tailor- (2026-07-16)
%
% Functional reconstruction of the standard "Medium Length Professional CV"
% resume.cls (LaTeXTemplates.com, orig. Trey Hunner). Implements the
% \name, \address, rSection, and rSubsection commands used by resume.tex.
% If you locate your original Overleaf resume.cls, prefer that file instead.
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{resume}[2026/07/16 Resume class]

\LoadClassWithOptions{article}

\RequirePackage{enumitem}

\renewcommand{\familydefault}{\sfdefault}

\pagestyle{empty}

\raggedbottom
\raggedright

\newcommand{\name}[1]{\def\@name{#1}}
\newcommand{\address}[1]{\def\@address{#1}}

\renewcommand{\maketitle}{
  \begin{center}
    {\huge \bf \MakeUppercase{\@name}} \\
    \smallskip
    \@address
  \end{center}
}

\newlength{\sectionspacing}
\setlength{\sectionspacing}{0.6em}

\newenvironment{rSection}[1]{
  \vspace{\sectionspacing}
  {\large \bf \MakeUppercase{#1}}
  \vspace{0.15em}
  \hrule
  \begin{list}{}{
    \setlength{\leftmargin}{1.5em}
    \setlength{\topsep}{0.15em}
  }
  \item[]
}{
  \end{list}
}

\newenvironment{rSubsection}[4]{
  \begin{tabular*}{\linewidth}{l@{\extracolsep{\fill}}r}
    \textbf{#1} & #2 \\
    \textit{#3} & \textit{#4} \\
  \end{tabular*}
  \vspace{-0.2em}
  \begin{list}{$\cdot$}{
    \setlength{\itemsep}{0em}
    \setlength{\parsep}{0em}
    \setlength{\leftmargin}{1.5em}
  }
  \vspace{-0.5\baselineskip}
}{
  \end{list}
  \vspace{0.2em}
}

\RequirePackage{tabularx}

\endinput
