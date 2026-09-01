import { useTranslation } from "react-i18next";

export const ProjectGraphLegend = () => {
  const { t } = useTranslation();
  return (
    <div
      className="project-graph__legend"
      aria-label={t("projectGraphs.legendAria")}
    >
      <span>
        <i className="is-info" />
        {t("projectGraphs.legendInfo")}
      </span>
      <span>
        <i className="is-success" />
        {t("projectGraphs.legendSuccess")}
      </span>
      <span>
        <i className="is-warning" />
        {t("projectGraphs.legendWarning")}
      </span>
      <span>
        <i className="is-danger" />
        {t("projectGraphs.legendDanger")}
      </span>
    </div>
  );
};
