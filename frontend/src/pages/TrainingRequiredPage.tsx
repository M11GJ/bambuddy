import { useTranslation } from 'react-i18next';
import { useAuth } from '../contexts/AuthContext';
import { Button } from '../components/Button';
import { isEmbedded } from '../utils/embedded';

export function TrainingRequiredPage() {
  const { t } = useTranslation();
  const { logout } = useAuth();
  return (
    <div className="min-h-screen flex flex-col items-center justify-center gap-6 p-6 text-center bg-bambu-dark text-white">
      <p className="max-w-md text-lg">{t('trainingRequired.message')}</p>
      {/* Inside the Hub, autologin would sign straight back in. */}
      {!isEmbedded() && <Button onClick={logout}>{t('trainingRequired.logout')}</Button>}
    </div>
  );
}
